"""A1: confirmation cards the Agent makes, and the reply after the App answers one.

v8 flow C (買回來了): the `complete_purchase` tool writes nothing. It reads the shopping
entries and the inventory items they add to, with their versions, makes one card line
per bought item (`purchase_operation` below; C1's preview checks each), and stores
a pending card through the approval service. The App shows the card; the write
happens only in `POST /agent/resume` after the user confirms
(`contracts/approval-lifecycle.md`): A1's `CardInputResolver`
(`services/purchase_inputs.py`) reads the stored lines back and C1's
`ShoppingWriteBuilder` plans the batch (Issue #60).

A list entry not linked to an inventory item is a thing the household does not have
yet (Issue #90): its line is a `create_item` line (`new_item_operation` below,
contract section 10) that adds a new inventory item with the confirmed name,
quantity, unit and location. When the household already has an item with that name
or alias, no line is made; the item-match card that would ask "is it this one?" has
no confirm rules yet.

Not covered yet: items that are not on the list, the item-match card (Issue #90),
buying more than listed, a different unit, a purchase time other than the
confirmation time, spreading more items than one batch holds over several cards,
changing a card (Issue #91), and reminding the user of open cards.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.agent import AgentResponse, AgentResult, ApprovalPresentation, ResultKind
from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus
from app.services.approvals import (
    ApprovalRecord,
    ApprovalService,
    ApprovalStatus,
    BatchTooLarge,
    NewOperation,
    ResumeOutcome,
    ResumeResult,
    TargetChanged,
    TargetVersion,
)
from app.services.cosmos_read import MappedDocument
from app.services.item_matching import match_term
from app.services.purchase_inputs import COMPLETE_PURCHASE, CREATE_ITEM, PurchaseRecordReader
from app.services.shopping.preview import ShoppingPreviewError, ShoppingPreviewService

logger = logging.getLogger(__name__)

PURCHASE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": COMPLETE_PURCHASE,
        "description": (
            "使用者說已經買了、買回來了待購清單上的東西時呼叫。"
            "只會出一張確認卡給使用者確認；使用者按確認之前，不會改庫存或待購清單。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "這次買回來的東西，一樣一項。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "shopping_item_id": {
                                "type": "string",
                                "description": "待購清單上那一筆的 shopping_item_id。",
                            },
                            "quantity": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "使用者說買了幾個，用待購清單上的單位；"
                                    "清單沒寫單位時，照使用者說的數量。"
                                ),
                            },
                            "unit": {
                                "type": "string",
                                "description": (
                                    "只有待購清單上那一筆沒有 item_id、清單也沒寫單位時才填："
                                    "使用者說的單位。"
                                ),
                            },
                            "location": {
                                "type": "string",
                                "description": (
                                    "只有待購清單上那一筆沒有 item_id（家裡還沒有這樣東西）時才填："
                                    "使用者說要放在哪裡。"
                                ),
                            },
                        },
                        "required": ["shopping_item_id", "quantity"],
                    },
                }
            },
            "required": ["items"],
        },
    },
}

NOT_ON_LIST = "待購清單上找不到這一筆，可能已經買過或被刪掉。"
NOT_LINKED = "這樣東西還沒有對應到庫存裡的品項，現在還不能從對話記錄購買。"
MORE_THAN_LISTED = "買的數量比待購清單上多，現在還不能記錄。"
OTHER_UNIT = "待購清單和庫存用的單位不同，現在還不能記錄。"
ALREADY_HAVE = "家裡已經有「{names}」，但待購清單上這一筆沒有連到它，現在還不能從對話記錄。"
NEEDS_UNIT = "待購清單上沒寫單位，請使用者說買了幾個什麼（例如 2 瓶）。"
NEEDS_LOCATION = "這是家裡還沒有的東西，要說放在哪裡，才能新增到庫存。"
ONE_CARD_PER_MESSAGE = json.dumps({"error": "card_already_made"})
"""A second `complete_purchase` in the same message: the first card stands."""
OPEN_CARD_FIRST = json.dumps({"error": "open_card_not_answered"})
"""`complete_purchase` while an earlier open card is shown again (section 8): no new card."""
OPEN_CARDS_UNKNOWN = json.dumps({"error": "open_cards_unknown"})
"""`complete_purchase` when the open cards could not be listed (section 8): no new card.

Unknown is not "none": a card could still be open, and a new one would be a second
card for the same purchase (PR #160 C2 review). The user says it again later.
"""
CARD_FAILED = json.dumps({"error": "card_not_made"})
TOO_MANY_ITEMS = json.dumps({"error": "too_many_items"})
RECORDS_CHANGED = json.dumps({"error": "records_changed"})
STILL_OPEN = "open"
"""`PurchaseCards.retire`: the old card is still pending, or could not be read."""


def purchase_operation(
    *,
    shopping: MappedDocument[ShoppingItem],
    inventory: MappedDocument[InventoryItem] | None,
    quantity: int,
    transaction_id: str | None = None,
) -> NewOperation:
    """One card line: "this shopping entry was bought, add it to this inventory item".

    C1's preview checks the line first, so a purchase the plan would refuse (more than
    listed, not linked to an inventory item, a different unit) never reaches a card;
    it raises `ShoppingDecisionRequired` or `ShoppingPreviewError`.

    A Shopping List entry may have no unit (Issue #52). The line then uses the linked
    inventory item's unit, shown on the card for the user to confirm; A1 supplies
    `purchased_unit` as agreed in Issue #60.
    """
    unit = None
    if shopping.record.unit is None and inventory is not None:
        unit = inventory.record.unit
    preview = ShoppingPreviewService().preview_purchase(
        household_id=shopping.record.household_id,
        shopping=shopping.record,
        inventory=None if inventory is None else inventory.record,
        purchased_quantity=quantity,
        purchased_unit=unit,
    )
    if inventory is None or shopping.etag is None or inventory.etag is None:
        raise ValueError("Both records must be read with their ETag.")
    name = shopping.record.name
    return NewOperation(
        action=COMPLETE_PURCHASE,
        description=f"{name} +{quantity} {preview.unit}（從待購清單完成購買）",
        values={
            "name": name,
            "purchased_quantity": quantity,
            "purchased_unit": preview.unit,
            "shopping_item_id": shopping.record.shopping_item_id,
            "item_id": inventory.record.item_id,
            "transaction_id": transaction_id or uuid.uuid4().hex,
        },
        targets=[
            TargetVersion(record_id=shopping.document_id, etag=shopping.etag),
            TargetVersion(record_id=inventory.document_id, etag=inventory.etag),
        ],
    )


def new_item_operation(
    *,
    shopping: MappedDocument[ShoppingItem],
    quantity: int,
    unit: str,
    location: str,
    transaction_id: str | None = None,
) -> NewOperation:
    """One card line: "this list entry was bought; add it as a new inventory item".

    Contract section 10 (Issue #90): `create_item` with `name`, `quantity`, `unit` and
    `location`, all shown on the card. The card never carries the new `item_id`; it is
    made from the line's `operation_id` when the user confirms. Only the shopping
    entry's version is on the line, because the new item does not exist yet.
    """
    if shopping.etag is None:
        raise ValueError("The shopping entry must be read with its ETag.")
    name = shopping.record.name
    return NewOperation(
        action=CREATE_ITEM,
        description=(
            f"會新增一個庫存品項：{name} {quantity} {unit}，放在{location}（從待購清單完成購買）"
        ),
        values={
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "location": location,
            "shopping_item_id": shopping.record.shopping_item_id,
            "transaction_id": transaction_id or uuid.uuid4().hex,
        },
        targets=[TargetVersion(record_id=shopping.document_id, etag=shopping.etag)],
    )


PHOTO_CARD_MADE = (
    "已出一張新增庫存品項的確認卡（家裡沒有同名或同別名的品項）。請使用者確認或取消；"
    "如果你判斷其中有家裡已經有的東西（只是名字不同），請提醒使用者可以按取消。"
)
"""Told to the model with the photo result when a new-item card was made (Issue #124)."""


def photo_item_operation(
    *, name: str, quantity: int, unit: str, transaction_id: str | None = None
) -> NewOperation:
    """One card line from a photo: "add this as a new inventory item" (Issue #124).

    Contract section 10: `create_item` with `name`, `quantity`, `unit` and `location`.
    A photo gives no location, so `location` is None and the App shows 未填; confirming
    accepts no location. There is no Shopping List entry, so no `shopping_item_id` and
    no target versions; `transaction_id` is for the write that confirms it.
    """
    return NewOperation(
        action=CREATE_ITEM,
        description=f"會新增一個庫存品項：{name} {quantity} {unit}（照片辨識）",
        values={
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "location": None,
            "transaction_id": transaction_id or uuid.uuid4().hex,
        },
    )


@dataclass(frozen=True)
class PhotoItem:
    """A photo item with no household item of the same name or alias (Issue #124)."""

    name: str
    quantity: int
    unit: str


class PhotoCards:
    """New-item cards from a photo (Issue #124, contract section 10).

    Off in the running app (`get_read_agent` passes none) until confirming one can be
    written: B1's write for a new item without a Shopping List entry, and A1 handing
    the card to it. Until then the Agent only reports what the photo shows.
    """

    def __init__(self, approvals: ApprovalService) -> None:
        self._approvals = approvals

    async def propose(self, household_id: str, items: Sequence[PhotoItem]) -> dict[str, Any] | None:
        """Store one pending card for the items and return it for the App, or None.

        One item is a `single_action` card, several a `batch_action` card. Nothing but
        the pending card is written; a card that cannot be stored is not shown.
        """
        if not items:
            return None
        lines = [
            photo_item_operation(name=item.name, quantity=item.quantity, unit=item.unit)
            for item in items
        ]
        presentation = (
            ApprovalPresentation.SINGLE_ACTION
            if len(lines) == 1
            else ApprovalPresentation.BATCH_ACTION
        )
        try:
            record = await self._approvals.save(
                self._approvals.prepare(
                    household_id=household_id, presentation=presentation, operations=lines
                )
            )
        except Exception:  # storage or planning failed; no card was stored
            logger.warning("Photo new-item card was not stored", exc_info=True)
            return None
        return ApprovalService.to_pending_approval(record).model_dump(mode="json")


class CardRecordReader(PurchaseRecordReader, Protocol):
    """B1's `CosmosReadRepository`: versioned reads, plus the inventory list for names."""

    async def list_inventory(self, household_id: str) -> list[InventoryItem]: ...


@dataclass(frozen=True)
class _Requested:
    shopping_item_id: str
    quantity: int
    unit: str | None = None
    location: str | None = None


class PurchaseCards:
    """The `complete_purchase` tool: makes and stores a pending purchase card."""

    def __init__(self, reader: CardRecordReader, approvals: ApprovalService) -> None:
        self._reader = reader
        self._approvals = approvals

    async def open_cards(self, household_id: str) -> list[ApprovalRecord] | None:
        """Pending cards the household has not answered, newest first (section 8).

        After 清除對話 or reopening the App a card is gone from the screen but still
        open here; the next reply shows the newest one again. Confirmed cards waiting
        for a retry are left out. None when the cards cannot be listed: the message is
        still answered, but it makes no new card (`OPEN_CARDS_UNKNOWN`).
        """
        try:
            cards = await self._approvals.list_open(household_id=household_id)
        except Exception:
            logger.warning("Open cards could not be listed", exc_info=True)
            return None
        pending = [card for card in cards if card.status is ApprovalStatus.PENDING]
        return sorted(pending, key=lambda card: card.created_at, reverse=True)

    async def card_to_change(self, household_id: str, approval_id: str) -> ApprovalRecord:
        """The pending card a 送出修改 replaces (contract section 7, Issue #91).

        Raises `CardNotChangeable` when it is missing, expired, confirmed or cancelled.
        """
        return await self._approvals.card_to_change(
            household_id=household_id, approval_id=approval_id
        )

    async def retire(
        self, household_id: str, old_approval_id: str, new_approval_id: str
    ) -> str | None:
        """After the new card is stored, the old one becomes `cancelled` (reason replaced).

        None when the old card is cancelled as `replaced`: only then does the new card
        count (`ApprovalService.counts`), so only then is it shown. Otherwise the new
        card is cancelled too (reason replace_failed) and the reason is returned: the
        old card's status (`expired`, `cancelled`, `confirmed`, `completed`) or
        `STILL_OPEN`. The reply then carries no card, so the user never holds two cards
        that can be confirmed (section 7, PR #143 review). If cancelling the new card
        also fails, it is logged; it does not count, so it is never listed or confirmed,
        and it expires.

        When the cancel raises, the old card is read back first: the cancel may have been
        written even though its reply was lost (commit then timeout, PR #143 B1 review).
        """
        try:
            result = await self._approvals.cancel_replaced(
                household_id=household_id, approval_id=old_approval_id
            )
            reason = _not_replaced_because(result.record)
            if reason is not None:
                logger.warning("Replaced card %s was not cancelled (%s)", old_approval_id, reason)
        except Exception:
            logger.warning("Replaced card %s was not cancelled", old_approval_id, exc_info=True)
            reason = await self._read_back(household_id, old_approval_id)
        if reason is None:
            return None
        try:
            await self._approvals.cancel_failed_replacement(
                household_id=household_id, approval_id=new_approval_id
            )
        except Exception:
            logger.error(
                "New card %s was not cancelled after its original %s was not replaced; "
                "it does not count, is not shown and expires on its own",
                new_approval_id,
                old_approval_id,
                exc_info=True,
            )
        return reason

    async def _read_back(self, household_id: str, approval_id: str) -> str | None:
        """After a cancel error: None if the old card reads back as replaced after all."""
        try:
            record = await self._approvals.read_card(
                household_id=household_id, approval_id=approval_id
            )
        except Exception:
            logger.warning("Replaced card %s could not be read back", approval_id, exc_info=True)
            return STILL_OPEN
        reason = _not_replaced_because(record)
        if reason is None:
            logger.info("Replaced card %s was cancelled after all", approval_id)
        return reason

    async def propose(
        self, household_id: str, args: dict[str, Any], *, replaces_approval_id: str | None = None
    ) -> tuple[str, dict[str, Any] | None]:
        """(result for the model, card for the App). Writes nothing but the pending card.

        `replaces_approval_id` is the card a 送出修改 replaces; the new card records it
        (section 7).
        """
        requested = _requested_items(args.get("items"))
        if not requested:
            return json.dumps({"error": "no_items"}), None
        lines: list[NewOperation] = []
        left_out: list[dict[str, Any]] = []
        for item in requested:
            line, reason = await self._line(household_id, item)
            if line is None:
                left_out.append({"shopping_item_id": item.shopping_item_id, "reason": reason})
            else:
                lines.append(line)
        if not lines:
            return json.dumps({"card": None, "not_on_card": left_out}, ensure_ascii=False), None

        presentation = (
            ApprovalPresentation.SINGLE_ACTION
            if len(lines) == 1
            else ApprovalPresentation.BATCH_ACTION
        )
        try:
            record = await self._approvals.save(
                self._approvals.prepare(
                    household_id=household_id,
                    presentation=presentation,
                    operations=lines,
                    replaces_approval_id=replaces_approval_id,
                )
            )
        except BatchTooLarge:
            return TOO_MANY_ITEMS, None
        except TargetChanged:
            return RECORDS_CHANGED, None
        except Exception:  # storage failed; no card was stored
            logger.warning("Purchase card was not stored", exc_info=True)
            return CARD_FAILED, None
        card = ApprovalService.to_pending_approval(record)
        view = {
            "card": {"lines": [line.description for line in lines]},
            "not_on_card": left_out,
        }
        return json.dumps(view, ensure_ascii=False), card.model_dump(mode="json")

    async def _line(self, household_id: str, item: _Requested) -> tuple[NewOperation | None, str]:
        quantity = item.quantity
        shopping = await self._reader.read_shopping_item_with_etag(
            household_id=household_id, shopping_item_id=item.shopping_item_id
        )
        if shopping is None or shopping.record.status is not ShoppingStatus.PENDING:
            return None, NOT_ON_LIST
        entry = shopping.record
        if not entry.item_id:
            return await self._new_item_line(household_id, shopping, item)
        if entry.quantity is not None and quantity > entry.quantity:
            return None, MORE_THAN_LISTED
        inventory = await self._reader.read_inventory_item_with_etag(
            household_id=household_id, item_id=entry.item_id
        )
        if inventory is None:
            return None, NOT_LINKED
        if entry.unit is not None and inventory.record.unit != entry.unit:
            return None, OTHER_UNIT
        try:
            return purchase_operation(shopping=shopping, inventory=inventory, quantity=quantity), ""
        except (ShoppingPreviewError, ValueError) as error:
            return None, f"這一筆現在還不能記錄：{error}"

    async def _new_item_line(
        self, household_id: str, shopping: MappedDocument[ShoppingItem], item: _Requested
    ) -> tuple[NewOperation | None, str]:
        """A list entry the household does not have yet becomes a `create_item` line."""
        entry = shopping.record
        if entry.quantity is not None and item.quantity > entry.quantity:
            return None, MORE_THAN_LISTED
        same = match_term(entry.name, await self._reader.list_inventory(household_id))
        if same.items:
            return None, ALREADY_HAVE.format(names="、".join(i.name for i in same.items))
        if entry.unit is not None and item.unit is not None and item.unit != entry.unit:
            return None, OTHER_UNIT
        unit = entry.unit or item.unit
        if unit is None:
            return None, NEEDS_UNIT
        if item.location is None:
            return None, NEEDS_LOCATION
        try:
            line = new_item_operation(
                shopping=shopping, quantity=item.quantity, unit=unit, location=item.location
            )
        except ValueError as error:
            return None, f"這一筆現在還不能記錄：{error}"
        return line, ""


def _not_replaced_because(record: ApprovalRecord | None) -> str | None:
    """None when the old card is cancelled as `replaced`; otherwise why it is not."""
    if record is None or record.status is ApprovalStatus.PENDING:
        return STILL_OPEN
    if record.is_replaced:
        return None
    return record.status.value


def _requested_items(value: Any) -> list[_Requested]:
    """The model's items, each shopping entry once, quantities as whole numbers ≥ 1.

    `unit` and `location` are optional words. Blank text counts as not said: the real
    model fills unused optional fields with "" (seen on Azure OpenAI, 2026-11-13).
    """
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    requested: list[_Requested] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        shopping_item_id = str(item.get("shopping_item_id") or "").strip()
        quantity = item.get("quantity")
        words = [item.get("unit"), item.get("location")]
        if (
            not shopping_item_id
            or shopping_item_id in seen
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 1
            or any(w is not None and not isinstance(w, str) for w in words)
        ):
            return []
        seen.add(shopping_item_id)
        unit, location = ((w or "").strip() or None for w in words)
        requested.append(_Requested(shopping_item_id, quantity, unit, location))
    return requested


RESUME_MESSAGES = {
    ResumeOutcome.CANCELLED: "已取消，沒有記錄任何東西。",
    ResumeOutcome.EXPIRED: "這張確認卡已經過期，沒有記錄。請再說一次要記錄的內容。",
    ResumeOutcome.NEEDS_NEW_CARD: (
        "庫存或待購清單剛剛被改過，這張卡沒有記錄。請再說一次，我重新出一張卡。"
    ),
    ResumeOutcome.NOT_WRITTEN: "這次沒有記錄成功，請再按一次確認。",
}


def resume_response(result: ResumeResult) -> AgentResponse:
    """What the App shows after the user answers a card. "Done" only when written (#52)."""
    if result.written and result.record is not None:
        return AgentResponse(
            message="已記錄：" + "、".join(_written_lines(result.record)),
            results=[
                AgentResult(
                    kind=ResultKind.ACTION_RESULT,
                    summary="已寫入",
                    data={
                        "approval_id": result.record.approval_id,
                        "outcome": result.outcome.value,
                        "operations": dict(result.record.outcome or {}),
                    },
                )
            ],
        )
    message = RESUME_MESSAGES.get(result.outcome)
    if message is None:
        raise RuntimeError(f"Unexpected resume outcome {result.outcome.value}.")
    return AgentResponse(
        message=message,
        results=[
            AgentResult(
                kind=ResultKind.ACTION_RESULT,
                summary=message,
                data={"outcome": result.outcome.value},
            )
        ],
    )


def _written_lines(record: ApprovalRecord) -> Sequence[str]:
    outcome = record.outcome or {}
    lines = []
    for operation in record.operations:
        written = outcome.get(operation.operation_id)
        if not isinstance(written, dict):
            continue
        line = f"{operation.values.get('name')} +{_number(written.get('quantity_added'))}"
        line += f" {written.get('unit')}（現在 {_number(written.get('inventory_quantity'))}"
        line += f" {written.get('unit')}）"
        lines.append(line)
    return lines


def _number(value: Any) -> Any:
    return int(value) if isinstance(value, float) and value.is_integer() else value
