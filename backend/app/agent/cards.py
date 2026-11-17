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

A purchase day the user says (「昨天買的」, A1-50) is built but off
(`dated_purchases`; `NESTOCK_DATED_PURCHASES=1` turns it on for local testing): the
App does not show it on the card yet, and a card may only hold values the user sees
(HITL rule 4).

Not covered yet: items that are not on the list, the item-match card (Issue #90),
buying more than listed, a different unit, spreading more items than one batch holds
over several cards, changing a card (Issue #91), and reminding the user of open cards.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from app.models.agent import (
    AgentResponse,
    AgentResult,
    ApprovalPresentation,
    CardChange,
    ResultKind,
)
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
from app.services.purchase_time import (
    TAIPEI,
    FuturePurchaseDate,
    resolve_purchased_at,
    taipei_today,
)
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

PURCHASED_ON_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "只有使用者說了哪天買的（例如「昨天買的」「11/3 買的」）才填："
        "那一天的日期，格式 YYYY-MM-DD。沒說就不要填。"
    ),
}
"""Offered to the model only with dated purchases on (A1-50); off in the running app."""


def _with_purchase_day(schema: dict[str, Any]) -> dict[str, Any]:
    dated = copy.deepcopy(schema)
    item = dated["function"]["parameters"]["properties"]["items"]["items"]
    item["properties"]["purchased_on"] = PURCHASED_ON_FIELD
    return dated


DATED_PURCHASE_TOOL_SCHEMA = _with_purchase_day(PURCHASE_TOOL_SCHEMA)
TODAY_NOTE = (
    "今天是 {today}（台北時間）。使用者說了哪天買的（例如昨天、11/3），"
    "呼叫 complete_purchase 時把那一天換成日期填進 purchased_on；沒說就不要填。"
)
"""Given to the model with dated purchases on, so it can turn a spoken day into a date."""

NOT_ON_LIST = "待購清單上找不到這一筆，可能已經買過或被刪掉。"
NOT_LINKED = "這樣東西還沒有對應到庫存裡的品項，現在還不能從對話記錄購買。"
OTHER_UNIT = "待購清單和庫存用的單位不同，現在還不能記錄。"
ALREADY_HAVE = "家裡已經有「{names}」，但待購清單上這一筆沒有連到它，現在還不能從對話記錄。"
NEEDS_UNIT = "待購清單上沒寫單位，請使用者說買了幾個什麼（例如 2 瓶）。"
NEEDS_LOCATION = "這是家裡還沒有的東西，要說放在哪裡，才能新增到庫存。"
UNREADABLE_DAY = "看不懂是哪一天買的，請使用者說日期（例如 11/15）。"
FUTURE_DAY = "購買日期在今天之後，不能記錄；請使用者再說一次是哪天買的。"

# 送出修改 sent as data (Issue #162): the reply when no new card is made.
CARD_CHANGED = "已照修改重新出卡，請再確認一次。"
CHANGE_UNKNOWN_LINE = "這一項不在原本的卡上，沒有出新卡。"
CHANGE_DUPLICATE_LINE = "同一項改了兩次，沒有出新卡。"
CHANGE_NOTHING = "卡上的值都沒有改，沒有出新卡。"
CHANGE_DAY_OFF = "現在還不能改購買日期，沒有出新卡。"
CHANGE_FUTURE_DAY = "購買日期不能在今天之後，沒有出新卡。"
CHANGE_NOT_THIS_CARD = "這張卡不能在卡上修改，沒有出新卡。"
CHANGE_NOT_THIS_LINE = "這一項不能在卡上修改。"
CHANGE_LINE_REFUSED = "沒有出新卡："
CHANGE_PHOTO_NO_DAY = "照片新增的品項沒有購買日期，不能改日期。"
CHANGE_PHOTO_HAVE_NOW = "家裡現在已經有「{names}」，不會再新增一個。"
CHANGE_RECORDS_MOVED = "庫存或待購清單剛剛被改過，請再改一次。"
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
    purchased_at: datetime | None = None,
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
        description=(
            f"{name} +{quantity} {preview.unit}（{_bought(purchased_at)}從待購清單完成購買）"
        ),
        values={
            "name": name,
            "purchased_quantity": quantity,
            "purchased_unit": preview.unit,
            "shopping_item_id": shopping.record.shopping_item_id,
            "item_id": inventory.record.item_id,
            "transaction_id": transaction_id or uuid.uuid4().hex,
            **_purchased_at_value(purchased_at),
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
    purchased_at: datetime | None = None,
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
            f"會新增一個庫存品項：{name} {quantity} {unit}，放在{location}"
            f"（{_bought(purchased_at)}從待購清單完成購買）"
        ),
        values={
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "location": location,
            "shopping_item_id": shopping.record.shopping_item_id,
            "transaction_id": transaction_id or uuid.uuid4().hex,
            **_purchased_at_value(purchased_at),
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

    @staticmethod
    def made(record: ApprovalRecord) -> bool:
        """Whether `record` is a photo new-item card: only `create_item` lines, no list entry."""
        return bool(record.operations) and all(
            op.action == CREATE_ITEM and not op.values.get("shopping_item_id")
            for op in record.operations
        )

    async def change_card(
        self,
        old: ApprovalRecord,
        changes: Sequence[CardChange],
        household_items: Sequence[InventoryItem],
    ) -> dict[str, Any]:
        """The new card for a 送出修改 on a photo card, sent as data (Issue #124, #162).

        Only `quantity` can change: a photo line has no purchase day. Every line goes on
        the new card with its name, unit and empty location, changed lines with their new
        quantity; each line gets a new `operation_id` and `transaction_id`, and the card
        records the one it replaces (section 7). A line whose name or alias the household
        has by now is refused, so confirming never adds a second item. Raises
        `CardChangeRefused` and stores nothing when a change does not fit; the caller
        retires `old` once the new card is stored.
        """
        by_id = {op.operation_id: op for op in old.operations}
        changed: dict[str, CardChange] = {}
        for change in changes:
            if change.operation_id not in by_id:
                raise CardChangeRefused(
                    "unknown_operation", CHANGE_UNKNOWN_LINE, operation_id=change.operation_id
                )
            if change.operation_id in changed:
                raise CardChangeRefused(
                    "duplicate_operation", CHANGE_DUPLICATE_LINE, operation_id=change.operation_id
                )
            changed[change.operation_id] = change
        dated = [by_id[c.operation_id] for c in changes if "purchased_on" in c.model_fields_set]
        if dated:
            _refuse_lines([(op, CHANGE_PHOTO_NO_DAY) for op in dated])
        items = []
        for op in old.operations:
            change = changed.get(op.operation_id)
            quantity = _whole(op.values.get("quantity"))
            if change is not None and change.quantity is not None:
                quantity = change.quantity
            items.append(
                PhotoItem(
                    name=str(op.values.get("name")),
                    quantity=quantity,
                    unit=str(op.values.get("unit")),
                )
            )
        if all(
            item.quantity == _whole(op.values.get("quantity"))
            for item, op in zip(items, old.operations, strict=True)
        ):
            raise CardChangeRefused("nothing_changed", CHANGE_NOTHING)
        have = []
        for item, op in zip(items, old.operations, strict=True):
            same = match_term(item.name, list(household_items))
            if same.items:
                names = "、".join(i.name for i in same.items)
                have.append((op, CHANGE_PHOTO_HAVE_NOW.format(names=names)))
        if have:
            _refuse_lines(have)
        lines = [
            photo_item_operation(name=item.name, quantity=item.quantity, unit=item.unit)
            for item in items
        ]
        try:
            record = await self._approvals.save(
                self._approvals.prepare(
                    household_id=old.household_id,
                    presentation=old.presentation,
                    operations=lines,
                    replaces_approval_id=old.approval_id,
                )
            )
        except Exception as error:  # storage failed; no card was stored
            raise RuntimeError("The changed photo card was not stored.") from error
        return ApprovalService.to_pending_approval(record).model_dump(mode="json")


def _bought(purchased_at: datetime | None) -> str:
    """「11/15 買的，」 on a dated line; nothing when the confirmation time is used."""
    if purchased_at is None:
        return ""
    day = purchased_at.astimezone(TAIPEI).date()
    return f"{day.month}/{day.day} 買的，"


def _purchased_at_value(purchased_at: datetime | None) -> dict[str, str]:
    """`purchased_at` for the line's values; `CardInputResolver` passes it on to C1."""
    return {} if purchased_at is None else {"purchased_at": purchased_at.isoformat()}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CardRecordReader(PurchaseRecordReader, Protocol):
    """B1's `CosmosReadRepository`: versioned reads, plus the inventory list for names."""

    async def list_inventory(self, household_id: str) -> list[InventoryItem]: ...


@dataclass(frozen=True)
class _Requested:
    shopping_item_id: str
    quantity: int
    unit: str | None = None
    location: str | None = None
    purchased_on: str | None = None


class PurchaseCards:
    """The `complete_purchase` tool: makes and stores a pending purchase card.

    `dated_purchases` (A1-50; off unless `NESTOCK_DATED_PURCHASES=1`, read in
    `read_agent._purchase_cards`): the model is told today's date and may give each
    item the day it was bought. A past day goes on the line as `purchased_at` (12:00
    Taipei that day, `purchase_time.py`) and in its text; today or no day leaves it
    off, so the confirmation time is used. Contract section 11 says when it is on.
    """

    def __init__(
        self,
        reader: CardRecordReader,
        approvals: ApprovalService,
        *,
        dated_purchases: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._reader = reader
        self._approvals = approvals
        self._dated = dated_purchases
        self._clock = clock

    @property
    def tool_schema(self) -> dict[str, Any]:
        """The tool offered to the model; no `purchased_on` while dated purchases are off."""
        return DATED_PURCHASE_TOOL_SCHEMA if self._dated else PURCHASE_TOOL_SCHEMA

    def date_note(self) -> str | None:
        """Today's date for the model with dated purchases on; None while they are off."""
        if not self._dated:
            return None
        return TODAY_NOTE.format(today=taipei_today(self._clock()).isoformat())

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
        requested = _requested_items(args.get("items"), dated=self._dated)
        if not requested:
            return json.dumps({"error": "no_items"}), None
        lines, left_out = await self._lines(household_id, requested)
        if not lines:
            return json.dumps({"card": None, "not_on_card": left_out}, ensure_ascii=False), None
        record, error = await self._store(household_id, lines, replaces_approval_id)
        if record is None:
            return error, None
        card = ApprovalService.to_pending_approval(record)
        view = {
            "card": {"lines": [line.description for line in lines]},
            "not_on_card": left_out,
        }
        return json.dumps(view, ensure_ascii=False), card.model_dump(mode="json")

    async def change_card(
        self, old: ApprovalRecord, changes: Sequence[CardChange]
    ) -> dict[str, Any]:
        """The new card for a 送出修改 sent as data (Issue #162); the model is not used.

        Every line of `old` goes on the new card as it was, with the changed fields
        replaced, read again from the current records (section 7). Raises
        `CardChangeRefused` and stores nothing when a change does not fit the card or
        any line can no longer be recorded: the new card never silently drops a line.
        The caller retires `old` once the new card is stored (`retire`).
        """
        by_id = {op.operation_id: op for op in old.operations}
        today = taipei_today(self._clock())
        changed: dict[str, CardChange] = {}
        for change in changes:
            if change.operation_id not in by_id:
                raise CardChangeRefused(
                    "unknown_operation", CHANGE_UNKNOWN_LINE, operation_id=change.operation_id
                )
            if change.operation_id in changed:
                raise CardChangeRefused(
                    "duplicate_operation", CHANGE_DUPLICATE_LINE, operation_id=change.operation_id
                )
            changed[change.operation_id] = change
            if "purchased_on" in change.model_fields_set:
                if not self._dated:
                    raise CardChangeRefused("purchase_day_off", CHANGE_DAY_OFF)
                if change.purchased_on is not None and change.purchased_on > today:
                    raise CardChangeRefused(
                        "future_date", CHANGE_FUTURE_DAY, operation_id=change.operation_id
                    )
        before = [_line_as_said(op) for op in old.operations]
        not_here = [op for op, item in zip(old.operations, before, strict=True) if item is None]
        if not_here:
            raise CardChangeRefused(
                "line_refused",
                CHANGE_NOT_THIS_CARD,
                lines=[_refused(op, CHANGE_NOT_THIS_LINE) for op in not_here],
            )
        if not self._dated and any("purchased_on" in item for item in before if item):
            raise CardChangeRefused("purchase_day_off", CHANGE_DAY_OFF)
        after = [
            _with_change(item, changed.get(op.operation_id), today)
            for op, item in zip(old.operations, before, strict=True)
            if item is not None
        ]
        if after == before:
            raise CardChangeRefused("nothing_changed", CHANGE_NOTHING)
        lines, left_out = await self._lines(
            old.household_id, _requested_items(after, dated=self._dated)
        )
        if left_out:
            by_entry = {str(op.values.get("shopping_item_id")): op for op in old.operations}
            _refuse_lines([(by_entry[row["shopping_item_id"]], row["reason"]) for row in left_out])
        record, error = await self._store(old.household_id, lines, old.approval_id)
        if record is None:
            if error == CARD_FAILED:
                raise RuntimeError("The changed card was not stored.")
            raise CardChangeRefused("line_refused", CHANGE_LINE_REFUSED + CHANGE_RECORDS_MOVED)
        return ApprovalService.to_pending_approval(record).model_dump(mode="json")

    async def _lines(
        self, household_id: str, requested: Sequence[_Requested]
    ) -> tuple[list[NewOperation], list[dict[str, Any]]]:
        """Card lines for the requested items, and the items left off with why."""
        lines: list[NewOperation] = []
        left_out: list[dict[str, Any]] = []
        for item in requested:
            line, reason = await self._line(household_id, item)
            if line is None:
                left_out.append({"shopping_item_id": item.shopping_item_id, "reason": reason})
            else:
                lines.append(line)
        return lines, left_out

    async def _store(
        self, household_id: str, lines: list[NewOperation], replaces_approval_id: str | None
    ) -> tuple[ApprovalRecord | None, str]:
        """Store the pending card: (record, "") or (None, the error for the model)."""
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
            return None, TOO_MANY_ITEMS
        except TargetChanged:
            return None, RECORDS_CHANGED
        except Exception:  # storage failed; no card was stored
            logger.warning("Purchase card was not stored", exc_info=True)
            return None, CARD_FAILED
        return record, ""

    def _purchase_time(self, item: _Requested) -> tuple[datetime | None, str]:
        """(purchased_at for the line, why the item is left off). Today means no time."""
        if item.purchased_on is None:
            return None, ""
        try:
            day = date.fromisoformat(item.purchased_on)
        except ValueError:
            return None, UNREADABLE_DAY
        now = self._clock()
        if day == taipei_today(now):
            return None, ""
        try:
            return resolve_purchased_at(day, now), ""
        except FuturePurchaseDate:
            return None, FUTURE_DAY

    async def _line(self, household_id: str, item: _Requested) -> tuple[NewOperation | None, str]:
        purchased_at, refused = self._purchase_time(item)
        if refused:
            return None, refused
        quantity = item.quantity
        shopping = await self._reader.read_shopping_item_with_etag(
            household_id=household_id, shopping_item_id=item.shopping_item_id
        )
        if shopping is None or shopping.record.status is not ShoppingStatus.PENDING:
            return None, NOT_ON_LIST
        entry = shopping.record
        if not entry.item_id:
            return await self._new_item_line(household_id, shopping, item, purchased_at)
        # Issue #178: the list's planned quantity does not cap what was bought.
        inventory = await self._reader.read_inventory_item_with_etag(
            household_id=household_id, item_id=entry.item_id
        )
        if inventory is None:
            return None, NOT_LINKED
        if entry.unit is not None and inventory.record.unit != entry.unit:
            return None, OTHER_UNIT
        try:
            line = purchase_operation(
                shopping=shopping, inventory=inventory, quantity=quantity, purchased_at=purchased_at
            )
            return line, ""
        except (ShoppingPreviewError, ValueError) as error:
            return None, f"這一筆現在還不能記錄：{error}"

    async def _new_item_line(
        self,
        household_id: str,
        shopping: MappedDocument[ShoppingItem],
        item: _Requested,
        purchased_at: datetime | None = None,
    ) -> tuple[NewOperation | None, str]:
        """A list entry the household does not have yet becomes a `create_item` line."""
        entry = shopping.record
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
                shopping=shopping,
                quantity=item.quantity,
                unit=unit,
                location=item.location,
                purchased_at=purchased_at,
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


def _requested_items(value: Any, *, dated: bool = False) -> list[_Requested]:
    """The model's items, each shopping entry once, quantities as whole numbers ≥ 1.

    `unit` and `location` are optional words, and so is `purchased_on`, which is read
    only with dated purchases on. Blank text counts as not said: the real model fills
    unused optional fields with "" (seen on Azure OpenAI, 2026-11-13).
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
        day = item.get("purchased_on") if dated else None
        words = [item.get("unit"), item.get("location"), day]
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
        unit, location, purchased_on = ((w or "").strip() or None for w in words)
        requested.append(_Requested(shopping_item_id, quantity, unit, location, purchased_on))
    return requested


class CardChangeRefused(Exception):
    """A 送出修改 sent as data was refused; nothing was stored (Issue #162).

    `code` is one of the codes in contract section 7; `details` go with it.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.details = details


def _refused(op: Any, reason: str) -> dict[str, Any]:
    """One refused line for `results[0].data.lines`: its `operation_id` on the card, the
    list entry for list lines (contract section 7.1), and why."""
    row: dict[str, Any] = {"operation_id": op.operation_id}
    if op.values.get("shopping_item_id"):
        row["shopping_item_id"] = op.values.get("shopping_item_id")
    row["reason"] = reason
    return row


def _refuse_lines(refused: Sequence[tuple[Any, str]]) -> None:
    """Raise `line_refused` naming each refused line with its reason; nothing is stored."""
    message = "".join(
        f"「{op.values.get('name') or op.values.get('shopping_item_id')}」{reason}"
        for op, reason in refused
    )
    raise CardChangeRefused(
        "line_refused",
        CHANGE_LINE_REFUSED + message,
        lines=[_refused(op, reason) for op, reason in refused],
    )


def _whole(value: Any) -> Any:
    """A whole number read back from JSON may come as 2.0."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _line_as_said(op: Any) -> dict[str, Any] | None:
    """A stored card line as the items `propose` takes; None for lines that cannot change.

    A purchase line gives its shopping entry and quantity; a new-item line from the
    list also its unit and location; a dated line its Taipei day (`purchased_on`).
    """
    values = op.values
    if op.action == COMPLETE_PURCHASE:
        item = {
            "shopping_item_id": values.get("shopping_item_id"),
            "quantity": _whole(values.get("purchased_quantity")),
        }
    elif op.action == CREATE_ITEM and values.get("shopping_item_id"):
        item = {
            "shopping_item_id": values.get("shopping_item_id"),
            "quantity": _whole(values.get("quantity")),
            "unit": values.get("unit"),
            "location": values.get("location"),
        }
    else:
        return None
    purchased_at = values.get("purchased_at")
    if isinstance(purchased_at, str):
        day = datetime.fromisoformat(purchased_at).astimezone(TAIPEI).date()
        item["purchased_on"] = day.isoformat()
    return item


def _with_change(item: dict[str, Any], change: CardChange | None, today: date) -> dict[str, Any]:
    """The line after the user's change; fields the change leaves out stay as they were."""
    if change is None:
        return item
    changed = dict(item)
    if change.quantity is not None:
        changed["quantity"] = change.quantity
    if "purchased_on" in change.model_fields_set:
        if change.purchased_on is None or change.purchased_on == today:
            changed.pop("purchased_on", None)  # back to today: the confirmation time
        else:
            changed["purchased_on"] = change.purchased_on.isoformat()
    return changed


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
