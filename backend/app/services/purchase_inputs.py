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

Not covered yet: removals (A1 has no removal card), the item-match card (Issue #90),
buying more than listed, and a different unit. C1's plan refuses the last two.

Reads only; never writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from app.models.domain import InventoryItem, ShoppingItem
from app.services.approvals import ApprovalRecord, StoredOperation, TargetChanged
from app.services.cosmos_read import MappedDocument
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.shopping.planning import NewInventoryTarget, PurchaseInput
from app.services.shopping.workflow import ResolvedShoppingChanges

COMPLETE_PURCHASE = "complete_purchase"
"""`action` of a purchase card line (v8 tool `complete_purchase`)."""
CREATE_ITEM = "create_item"
"""`action` of a line that buys a Shopping List entry the household does not have yet."""


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
        purchases = [
            await self._purchase_input(record, operation)
            for operation in record.operations
            if operation.operation_id in approved
        ]
        return ResolvedShoppingChanges(purchases=tuple(purchases))

    async def _purchase_input(
        self, record: ApprovalRecord, operation: StoredOperation
    ) -> PurchaseInput:
        if operation.action == CREATE_ITEM:
            return await self._new_item_input(record, operation)
        if operation.action != COMPLETE_PURCHASE:
            raise UnsupportedAction(f"No purchase input for action {operation.action!r}.")
        values = operation.values
        shopping_item_id = _text(values, "shopping_item_id")
        item_id = _text(values, "item_id")

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
        if {target.record_id for target in operation.targets} != expected or len(
            operation.targets
        ) != len(expected):
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
            purchased_quantity=_count(values, "purchased_quantity"),
            purchased_unit=_text(values, "purchased_unit"),
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
        if [target.record_id for target in operation.targets] != [
            f"shopping_item:{shopping_item_id}"
        ]:
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
