"""A1's `ShoppingInputResolver` for 買回來了 (v8 flow C), Issue #60.

C1's `ShoppingWriteBuilder` plans the batch for a confirmed purchase card. Before it
can, someone has to turn the stored card back into C1's typed `PurchaseInput`; C1
asked A1 to do that part (Issue #60, Issue #27). This module is only that step:

- Only the operations in the card's fixed `approved_operation_ids` are resolved, in
  card order. Nothing comes from the App; `/agent/resume` only picks the card and
  its lines.
- Each line is a `complete_purchase` line in the format A1's cards use
  (`purchase_operation` in `app/agent/cards.py`): `purchased_quantity`, `purchased_unit`,
  `shopping_item_id`, `item_id`, `transaction_id`, and the version (ETag) of the
  shopping entry and the inventory item when the card was made. An optional
  `purchased_at` is passed through; without it C1 uses `decided_at`.
- Both records are read again through B1's repository. If either changed or was
  deleted since the card was made, `TargetChanged` is raised, so the approval
  service cancels the card and the Agent makes a new one. Nothing is re-matched.
- A `create_item` line (Issue #90, contract section 10) is a purchase of a Shopping
  List entry the household does not have yet: `name`, `quantity`, `unit`,
  `location`, `shopping_item_id`, `transaction_id`, and the version of the shopping
  entry only. It becomes C1's `NewInventoryTarget`; the new item's `item_id` comes
  from B1's `resolve_inventory_item_id(operation_id)`, so a retry gets the same item.
- A `confirm_item_match` line (contract section 10, Issue #175) is on an item-match
  card answered 「是」: a purchase of a Shopping List entry that is not linked yet,
  into the household item the card asked about. `candidate_item_id` is the target,
  `quantity` and `unit` what was bought, with the same two versions as a purchase
  line; the router (`card_writes.py`) sends only 「是」 here.

- A line may carry the photo the user sent with the message (Issue #176): its
  `image_ref`, `input_id` and `mapping_etag`, fixed when the card was made, plus the
  target `input:<input_id>` at that ETag. `stored_photo` reads them: all three or
  none, and only some of them raises `ValueError`, so nothing is written. Each line
  with a photo becomes one C1 `MediaAttachment`, taken from the card as it is: the
  mapping is not looked up again and its ETag is not refreshed, so a retry hands
  over the same values. Without a photo the purchase is resolved as before.

Not covered yet: removals (A1 has no removal card) and a different unit, which C1's
plan refuses. Buying more than listed is resolved like any other line; the listed
quantity is not a limit (Issue #178).

Photo new-item cards (Issue #124, contract section 10) have no Shopping List entry,
so they are not purchases: `PhotoInputResolver` below turns them into C1's
`AdjustmentInput` instead, for B1's write of a new item without a Shopping List entry.

Reads only; never writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from app.models.domain import InventoryItem, ShoppingItem
from app.services.approvals import (
    CONFIRM_ITEM_MATCH,
    ApprovalRecord,
    StoredOperation,
    TargetChanged,
)
from app.services.cosmos_read import MappedDocument
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.shopping.media import MediaAttachment
from app.services.shopping.planning import AdjustmentInput, NewInventoryTarget, PurchaseInput
from app.services.shopping.workflow import ResolvedShoppingChanges

COMPLETE_PURCHASE = "complete_purchase"
"""`action` of a purchase card line (v8 tool `complete_purchase`)."""
CREATE_ITEM = "create_item"
"""`action` of a line that buys a Shopping List entry the household does not have yet."""

IMAGE_REF = "image_ref"
INPUT_ID = "input_id"
MAPPING_ETAG = "mapping_etag"
"""The three photo values a card line holds (Issue #176); B1's photo writer reads the same."""


def stored_photo(values: Mapping[str, object]) -> tuple[str, str, str] | None:
    """(image_ref, input_id, mapping_etag) on a card line, or None without a photo.

    Issue #176. All three or none, the same rule as B1's photo new-item writer: a line
    with only some of them raises `ValueError`, so nothing is written (fail closed).
    """
    image_ref, input_id, mapping_etag = (values.get(k) for k in (IMAGE_REF, INPUT_ID, MAPPING_ETAG))
    if image_ref is None and input_id is None and mapping_etag is None:
        return None
    if not (
        isinstance(image_ref, str)
        and image_ref.strip()
        and isinstance(input_id, str)
        and input_id.strip()
        and isinstance(mapping_etag, str)
        and mapping_etag.strip()
    ):
        raise ValueError("A card photo needs image_ref, input_id and mapping_etag together.")
    return image_ref, input_id, mapping_etag


def photo_target(input_id: str) -> str:
    """The `record_id` of the photo's mapping among a purchase line's targets."""
    return f"input:{input_id}"


class UnsupportedAction(ValueError):
    """The card has an operation this resolver does not turn into C1 input."""


class PurchaseRecordReader(Protocol):
    """The part of B1's `CosmosReadRepository` this resolver uses (PR #72)."""

    async def read_shopping_item_with_etag(
        self, *, household_id: str, shopping_item_id: str
    ) -> MappedDocument[ShoppingItem] | None: ...

    async def read_inventory_item_with_etag(
        self, *, household_id: str, item_id: str
    ) -> MappedDocument[InventoryItem] | None: ...


class CardInputResolver:
    """`ShoppingInputResolver` over A1's stored purchase cards (Issue #60)."""

    def __init__(self, reader: PurchaseRecordReader) -> None:
        self._reader = reader

    async def resolve(self, record: ApprovalRecord) -> ResolvedShoppingChanges:
        approved = set(record.approved_operation_ids or ())
        selected = [op for op in record.operations if op.operation_id in approved]
        attachments = tuple(
            attachment for op in selected if (attachment := _attachment(op)) is not None
        )
        purchases = [await self._purchase_input(record, operation) for operation in selected]
        return ResolvedShoppingChanges(purchases=tuple(purchases), media_attachments=attachments)

    async def _purchase_input(
        self, record: ApprovalRecord, operation: StoredOperation
    ) -> PurchaseInput:
        if operation.action == CREATE_ITEM:
            return await self._new_item_input(record, operation)
        if operation.action == COMPLETE_PURCHASE:
            return await self._existing_item_input(
                record, operation, "item_id", "purchased_quantity", "purchased_unit"
            )
        if operation.action == CONFIRM_ITEM_MATCH:
            return await self._existing_item_input(
                record, operation, "candidate_item_id", "quantity", "unit"
            )
        raise UnsupportedAction(f"No purchase input for action {operation.action!r}.")

    async def _existing_item_input(
        self,
        record: ApprovalRecord,
        operation: StoredOperation,
        item_key: str,
        quantity_key: str,
        unit_key: str,
    ) -> PurchaseInput:
        """A purchase into an existing inventory item; both records must be unchanged."""
        values = operation.values
        shopping_item_id = _text(values, "shopping_item_id")
        item_id = _text(values, item_key)

        shopping, shopping_etag = _unchanged(
            operation,
            await self._reader.read_shopping_item_with_etag(
                household_id=record.household_id, shopping_item_id=shopping_item_id
            ),
        )
        inventory, inventory_etag = _unchanged(
            operation,
            await self._reader.read_inventory_item_with_etag(
                household_id=record.household_id, item_id=item_id
            ),
        )
        expected = {f"shopping_item:{shopping_item_id}", f"inventory_item:{item_id}"}
        if not _has_targets(operation, expected):
            raise ValueError(
                f"{operation.operation_id} must carry exactly its shopping and inventory versions."
            )

        return PurchaseInput(
            operation_id=operation.operation_id,
            transaction_id=_text(values, "transaction_id"),
            shopping=shopping,
            shopping_etag=shopping_etag,
            inventory=inventory,
            inventory_etag=inventory_etag,
            purchased_quantity=_count(values, quantity_key),
            purchased_unit=_text(values, unit_key),
            resolved_inventory_item_id=item_id,
            purchased_at=_optional_time(values, "purchased_at"),
        )

    async def _new_item_input(
        self, record: ApprovalRecord, operation: StoredOperation
    ) -> PurchaseInput:
        values = operation.values
        shopping_item_id = _text(values, "shopping_item_id")
        shopping, shopping_etag = _unchanged(
            operation,
            await self._reader.read_shopping_item_with_etag(
                household_id=record.household_id, shopping_item_id=shopping_item_id
            ),
        )
        if not _has_targets(operation, {f"shopping_item:{shopping_item_id}"}):
            raise ValueError(
                f"{operation.operation_id} must carry exactly its shopping entry's version."
            )
        item_id = resolve_inventory_item_id(operation.operation_id)
        unit = _text(values, "unit")
        return PurchaseInput(
            operation_id=operation.operation_id,
            transaction_id=_text(values, "transaction_id"),
            shopping=shopping,
            shopping_etag=shopping_etag,
            inventory=None,
            inventory_etag=None,
            purchased_quantity=_count(values, "quantity"),
            purchased_unit=unit,
            resolved_inventory_item_id=item_id,
            purchased_at=_optional_time(values, "purchased_at"),
            new_inventory=NewInventoryTarget(
                item_id=item_id,
                name=_text(values, "name"),
                unit=unit,
                location=_text(values, "location"),
            ),
        )


def is_photo_card(record: ApprovalRecord) -> bool:
    """A photo new-item card (Issue #124): only `create_item` lines, no Shopping List entry."""
    return bool(record.operations) and all(
        operation.action == CREATE_ITEM and not operation.values.get("shopping_item_id")
        for operation in record.operations
    )


class PhotoInputResolver:
    """C1's `AdjustmentInput` for each confirmed line of a photo new-item card (Issue #124).

    Only the operations in the card's fixed `approved_operation_ids`, in card order.
    Everything comes from the stored card, never from the App (C1's `AdjustmentInput`
    rule): `approval_id`; per line its `operation_id`, `transaction_id`, `quantity`,
    `name`, `unit` and `location` (null unless said or filled in on the card, Issue
    #188). The new item's `item_id` is B1's `resolve_inventory_item_id(operation_id)`,
    so a retry gets the same item. The write time is the card's `decided_at`, which the
    builder takes from the record (Issue #124, 2026-11-16). A photo line carries no
    record version, so nothing is read.
    """

    def resolve(self, record: ApprovalRecord) -> tuple[AdjustmentInput, ...]:
        if not is_photo_card(record):
            raise UnsupportedAction("Not a photo new-item card.")
        approved = set(record.approved_operation_ids or ())
        return tuple(
            self._adjustment(record, operation)
            for operation in record.operations
            if operation.operation_id in approved
        )

    def _adjustment(self, record: ApprovalRecord, operation: StoredOperation) -> AdjustmentInput:
        if operation.targets:
            raise ValueError(f"{operation.operation_id} is a photo line; it has no versions.")
        values = operation.values
        item_id = resolve_inventory_item_id(operation.operation_id)
        return AdjustmentInput(
            operation_id=operation.operation_id,
            transaction_id=_text(values, "transaction_id"),
            approval_id=record.approval_id,
            new_inventory=NewInventoryTarget(
                item_id=item_id,
                name=_text(values, "name"),
                unit=_text(values, "unit"),
                location=_optional_text(values, "location"),
            ),
            quantity=_count(values, "quantity"),
        )


def _attachment(operation: StoredOperation) -> MediaAttachment | None:
    """C1's attachment for a line's photo, exactly as the card holds it (Issue #176)."""
    photo = stored_photo(operation.values)
    if photo is None:
        return None
    image_ref, input_id, mapping_etag = photo
    return MediaAttachment(
        operation_id=operation.operation_id,
        input_id=input_id,
        image_ref=image_ref,
        mapping_etag=mapping_etag,
    )


def _has_targets(operation: StoredOperation, records: set[str]) -> bool:
    """Whether the line carries exactly `records`, plus its photo's mapping if it has one.

    The photo's target must be at the ETag the line holds (Issue #176).
    """
    expected = {record_id: None for record_id in records}
    photo = stored_photo(operation.values)
    if photo is not None:
        _, input_id, mapping_etag = photo
        expected[photo_target(input_id)] = mapping_etag
    shown = {target.record_id: target.etag for target in operation.targets}
    return (
        len(operation.targets) == len(expected)
        and shown.keys() == expected.keys()
        and all(etag is None or shown[key] == etag for key, etag in expected.items())
    )


def _unchanged[RecordT: (ShoppingItem, InventoryItem)](
    operation: StoredOperation, current: MappedDocument[RecordT] | None
) -> tuple[RecordT, str]:
    """The record and its ETag, if it is still the version on the card.

    A deleted or missing record counts as changed (contract section 5, point 6).
    """
    if current is None or current.etag is None:
        raise TargetChanged(f"A record on {operation.operation_id} is gone.")
    shown = {target.record_id: target.etag for target in operation.targets}
    if current.document_id not in shown:
        raise ValueError(f"The card has no version for {current.document_id}.")
    if shown[current.document_id] != current.etag:
        raise TargetChanged(current.document_id)
    return current.record, current.etag


def _text(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"The card's {key} must be a non-empty string.")
    return value


def _optional_text(values: Mapping[str, object], key: str) -> str | None:
    """None when the card left it empty (a photo line's `location`), else non-empty text."""
    if values.get(key) is None:
        return None
    return _text(values, key)


def _count(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"The card's {key} must be a positive whole number.")
    return value


def _optional_time(values: Mapping[str, object], key: str) -> datetime | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"The card's {key} must be an ISO 8601 time.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"The card's {key} must be an ISO 8601 time.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"The card's {key} must include a time zone.")
    return parsed
