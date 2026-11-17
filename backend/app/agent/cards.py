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
quantity, unit and location. When it may be a household item after all, the card is
an item-match card instead (`item_match_operation`, contract section 10, Issue #175):
"這是家裡的洗潔精嗎？". That is the case when the model names the item it thinks it is
(`candidate_item_id`, a different name for the same thing) or when exactly one
household item has the entry's name or alias. 「是」 writes the purchase into that
item, as a purchase card would, and remembers no alias; 「不是」 writes nothing. An
item-match card holds one item only: with other items in the same message, the
ordinary items go on the card and the user is asked to say the unsure one again
afterwards.

A purchase day the user says (「昨天買的」, A1-50) is built but off
(`dated_purchases`; `NESTOCK_DATED_PURCHASES=1` turns it on for local testing): the
App does not show it on the card yet, and a card may only hold values the user sees
(HITL rule 4).

Buying more than listed makes a card with what was bought; the listed quantity is not
a limit (Issue #178).

A photo sent with the message is kept with the record it adds to (Issue #176):
photo new-item lines, and a purchase card with exactly one line, hold the photo's
`image_ref`, `input_id` and the ETag of its completed mapping (`CardPhoto`), fixed when
the card is made; a purchase line also carries the mapping as a target. Confirming
hands exactly these on (B1's photo writer, C1's purchase batch); they are never looked
up again. The App does not show the photo on the card yet (C2, Issue #176), so the
model is told to say the photo will be kept (`PHOTO_KEPT`). No photo is kept, and the
model is told why, when the mapping is not completed, the item already has a photo
(it is never replaced), the purchase card has several lines, or the card is an
item-match card. Said new-item cards (`add_stock`) keep no photo.

Not covered yet: items that are not on the list, a different unit, spreading more
items than one batch holds over several cards, changing a card (Issue #91), and
reminding the user of open cards.
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
    CONFIRM_ITEM_MATCH,
    NEW_ITEM,
    SAME_ITEM,
    ApprovalRecord,
    ApprovalService,
    ApprovalStatus,
    BatchTooLarge,
    CancelReason,
    NewOperation,
    NewOption,
    ResumeOutcome,
    ResumeResult,
    TargetChanged,
    TargetVersion,
)
from app.services.cosmos_read import MappedDocument
from app.services.item_matching import match_term, normalize_term
from app.services.purchase_inputs import (
    COMPLETE_PURCHASE,
    CREATE_ITEM,
    IMAGE_REF,
    INPUT_ID,
    MAPPING_ETAG,
    PurchaseRecordReader,
    photo_target,
    stored_photo,
)
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
                            "candidate_item_id": {
                                "type": "string",
                                "description": (
                                    "只有待購清單上那一筆沒有 item_id，而你查過庫存、判斷它可能就是"
                                    "家裡已有的某一樣（名字不同，例如洗碗精和洗潔精）時才填："
                                    "家裡那一樣的 item_id。會出一張比對卡問使用者是不是同一樣。"
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
NO_SUCH_ITEM = "家裡的庫存找不到 candidate_item_id 這個品項；請重新查庫存。"
MATCH_LATER = (
    "可能是家裡的「{candidate}」，這張卡先不放這一樣。"
    "請使用者確認完這張卡之後，再說一次這一樣，會出比對卡問是不是同一樣。"
)
ITEM_MATCH_MADE = (
    "已出一張比對卡，問使用者「{term}」是不是家裡的「{candidate}」。"
    "請使用者在卡上選「是」或「不是」：選「是」才會記到{candidate}；"
    "選「不是」不會記錄，要新增成新的品項，使用者要再說一次買了幾個、放在哪裡。"
)
NOT_SAME_ITEM_REPLY = (
    "好，「{term}」不是家裡的「{candidate}」，沒有記錄任何東西。"
    "要新增成新的品項，請再說一次買了幾個、放在哪裡（例如「{term} {quantity} {unit}，放在廚房」）。"
)
NOT_SAME_ITEM_SAME_NAME = (
    "好，沒有記錄任何東西。家裡已經有同名的「{candidate}」，不會再新增一個同名的品項。"
)
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
CHANGE_SAME_NAME_TWICE = "同一張卡上有兩項都叫「{name}」，請改成不同的名字。"
CHANGE_NO_NAME_OR_PLACE = "這一項不能改名稱或位置。"
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

# Issue #176: told to the model with a card when the message had a photo (`photo`).
PHOTO_KEPT = (
    "使用者按確認後，這張照片會跟著這筆紀錄存下來（之後可以拿來比對，App 的庫存畫面不顯示）。"
    "請在回覆裡告訴使用者：確認後會把這張照片存下來。"
)
PHOTO_NOT_READY = "這張照片現在讀不到處理結果，這張卡不會存照片。請告訴使用者這次的照片不會存下來。"
PHOTO_ITEM_HAS_ONE = "「{name}」已經有照片，不會換掉，這次的照片不會存。請告訴使用者。"
PHOTO_ONE_LINE_ONLY = (
    "照片只跟著只有一項的購買卡存；這張卡有好幾項，這次的照片不會存。請告訴使用者。"
)
PHOTO_NOT_ON_MATCH = "比對卡不存照片，這次的照片不會存。請告訴使用者。"

CARD_FAILED = json.dumps({"error": "card_not_made"})
TOO_MANY_ITEMS = json.dumps({"error": "too_many_items"})
RECORDS_CHANGED = json.dumps({"error": "records_changed"})
STILL_OPEN = "open"
"""`PurchaseCards.retire`: the old card is still pending, or could not be read."""


@dataclass(frozen=True)
class CardPhoto:
    """The photo sent with the message, for a card to keep (Issue #176).

    `input_id` and `mapping_etag` come from the photo's mapping when it is completed;
    without them the photo cannot be kept (`ready` is False). Fixed on the card when it
    is made, so confirming keeps exactly the photo the card was made with.
    """

    image_ref: str
    input_id: str | None = None
    mapping_etag: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.input_id) and bool(self.mapping_etag)

    def values(self) -> dict[str, str]:
        """The line values for this photo; empty when it cannot be kept."""
        if not self.ready:
            return {}
        return {
            IMAGE_REF: self.image_ref,
            INPUT_ID: str(self.input_id),
            MAPPING_ETAG: str(self.mapping_etag),
        }

    @classmethod
    def on_line(cls, op: Any) -> CardPhoto | None:
        """The photo a stored line holds, or None (so 送出修改 keeps it)."""
        photo = stored_photo(op.values)
        if photo is None:
            return None
        image_ref, input_id, mapping_etag = photo
        return cls(image_ref=image_ref, input_id=input_id, mapping_etag=mapping_etag)


def with_photo(line: NewOperation, photo: CardPhoto) -> NewOperation:
    """A purchase line that also keeps `photo`: its values and its mapping as a target.

    C1 checks the target against the mapping's ETag when the card is confirmed.
    """
    return NewOperation(
        action=line.action,
        description=line.description,
        values={**line.values, **photo.values()},
        targets=[
            *line.targets,
            TargetVersion(
                record_id=photo_target(str(photo.input_id)), etag=str(photo.mapping_etag)
            ),
        ],
    )


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


def item_match_operation(
    *,
    shopping: MappedDocument[ShoppingItem],
    inventory: MappedDocument[InventoryItem],
    quantity: int,
    transaction_id: str | None = None,
    purchased_at: datetime | None = None,
) -> NewOperation:
    """The one line of an item-match card: "這是家裡的洗潔精嗎？" (Issue #175).

    Contract section 10. Shown: `user_term` (the list entry's name), `candidate_name`,
    `quantity` and `unit`; 「是」 approves exactly these. Fixed but not shown:
    `candidate_item_id`, `shopping_item_id`, `transaction_id` and both versions, so
    「是」 writes the same purchase a purchase card would, into the candidate item.
    C1's preview checks the purchase first, with the candidate as its resolved target;
    a purchase it would refuse never reaches a card. No alias is remembered.
    """
    if shopping.etag is None or inventory.etag is None:
        raise ValueError("Both records must be read with their ETag.")
    unit = inventory.record.unit if shopping.record.unit is None else None
    preview = ShoppingPreviewService().preview_purchase(
        household_id=shopping.record.household_id,
        shopping=shopping.record,
        inventory=inventory.record,
        purchased_quantity=quantity,
        purchased_unit=unit,
        resolved_inventory_item_id=inventory.record.item_id,
    )
    candidate = inventory.record.name
    bought = _bought(purchased_at).rstrip("，")
    return NewOperation(
        action=CONFIRM_ITEM_MATCH,
        description=f"這是家裡的{candidate}嗎？" + (f"（{bought}）" if bought else ""),
        values={
            "user_term": shopping.record.name,
            "candidate_item_id": inventory.record.item_id,
            "candidate_name": candidate,
            "quantity": quantity,
            "unit": preview.unit,
            "shopping_item_id": shopping.record.shopping_item_id,
            "transaction_id": transaction_id or uuid.uuid4().hex,
            **_purchased_at_value(purchased_at),
        },
        targets=[
            TargetVersion(record_id=shopping.document_id, etag=shopping.etag),
            TargetVersion(record_id=inventory.document_id, etag=inventory.etag),
        ],
    )


def item_match_options(candidate_name: str) -> list[NewOption]:
    """Exactly 「是」 and 「不是」 (contract section 10); the App shows each `label`."""
    return [
        NewOption(option_id=SAME_ITEM, values={"label": f"是，加到{candidate_name}"}),
        NewOption(option_id=NEW_ITEM, values={"label": "不是，這是新的東西"}),
    ]


PHOTO_CARD_MADE = (
    "已出一張新增庫存品項的確認卡（家裡沒有同名或同別名的品項）。請使用者確認或取消；"
    "如果你判斷其中有家裡已經有的東西（只是名字不同），請提醒使用者可以按取消。"
)
"""Told to the model with the photo result when a new-item card was made (Issue #124)."""


def photo_item_operation(
    *,
    name: str,
    quantity: int,
    unit: str,
    location: str | None = None,
    transaction_id: str | None = None,
    photo: CardPhoto | None = None,
) -> NewOperation:
    """One card line from a photo: "add this as a new inventory item" (Issue #124).

    Contract section 10: `create_item` with `name`, `quantity`, `unit` and `location`.
    A photo gives no location, so `location` is None and the App shows 未填 until the
    user fills one in on the card (Issue #188); confirming accepts no location. There
    is no Shopping List entry, so no `shopping_item_id` and no target versions;
    `transaction_id` is for the write that confirms it. A ready `photo` adds its three
    values (Issue #176); B1's writer checks them against the mapping when confirmed.
    """
    place = f"，放在{location}" if location else ""
    return NewOperation(
        action=CREATE_ITEM,
        description=f"會新增一個庫存品項：{name} {quantity} {unit}{place}（照片辨識）",
        values={
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "location": location,
            "transaction_id": transaction_id or uuid.uuid4().hex,
            **(photo.values() if photo is not None else {}),
        },
    )


SAID = "（使用者說的）"
"""Ends the description of a new-item line the user said (Issue #168), not a photo's."""


def said_item_operation(
    *,
    name: str,
    quantity: int,
    unit: str,
    location: str | None,
    transaction_id: str | None = None,
) -> NewOperation:
    """One card line the user said: "add this as a new inventory item" (Issue #168).

    The `add_stock` tool (`app/agent/stock_cards.py`) makes it: same `create_item`
    format as a photo line (contract section 10), with the location the user said or
    None (the App shows 未填). No Shopping List entry and no target versions, so it is
    confirmed through the photo path: `PhotoInputResolver` and B1's writer.
    """
    place = f"，放在{location}" if location else ""
    return NewOperation(
        action=CREATE_ITEM,
        description=f"會新增一個庫存品項：{name} {quantity} {unit}{place}{SAID}",
        values={
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "location": location,
            "transaction_id": transaction_id or uuid.uuid4().hex,
        },
    )


@dataclass(frozen=True)
class _NewItemLine:
    """What a photo or said new-item line shows: the values 送出修改 can change."""

    name: str
    quantity: int
    location: str | None


def _new_item_line(op: Any) -> _NewItemLine:
    location = op.values.get("location")
    return _NewItemLine(
        name=str(op.values.get("name")),
        quantity=_whole(op.values.get("quantity")),
        location=location if isinstance(location, str) else None,
    )


def _with_new_item_change(line: _NewItemLine, change: CardChange | None) -> _NewItemLine:
    """The line with a 送出修改 applied: a field left out keeps its value (Issue #188)."""
    if change is None:
        return line
    given = change.model_fields_set
    return _NewItemLine(
        name=change.name if "name" in given and change.name else line.name,
        quantity=change.quantity if change.quantity is not None else line.quantity,
        location=change.location if "location" in given else line.location,
    )


def _new_item_again(op: Any, line: _NewItemLine) -> NewOperation:
    """A photo or said new-item line again with `line`'s values, keeping its unit,
    where it came from (the description's source label) and its photo (Issue #176)."""
    if op.description.endswith(SAID):
        return said_item_operation(
            name=line.name,
            quantity=line.quantity,
            unit=str(op.values.get("unit")),
            location=line.location,
        )
    return photo_item_operation(
        name=line.name,
        quantity=line.quantity,
        unit=str(op.values.get("unit")),
        location=line.location,
        photo=CardPhoto.on_line(op),
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

    async def propose(
        self,
        household_id: str,
        items: Sequence[PhotoItem],
        photo: CardPhoto | None = None,
    ) -> dict[str, Any] | None:
        """Store one pending card for the items and return it for the App, or None.

        One item is a `single_action` card, several a `batch_action` card. Nothing but
        the pending card is written; a card that cannot be stored is not shown. With a
        ready `photo`, every line keeps it (Issue #176): the items share the one photo.
        """
        if not items:
            return None
        lines = [
            photo_item_operation(
                name=item.name, quantity=item.quantity, unit=item.unit, photo=photo
            )
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
        """Whether `record` is a new-item card without a list entry: only `create_item`
        lines with no `shopping_item_id`, from a photo or said by the user (Issue #168)."""
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
        """The new card for a 送出修改 on a photo or said new-item card (Issue #124, #162,
        #168, #188), sent as data.

        `name`, `quantity` and `location` can change, together in one change; a
        `purchased_on` is refused, as these lines have no purchase day. Every line goes on
        the new card with its unit and source, changed lines with their new values, the
        rest as they were; each line gets a new `operation_id` and `transaction_id`, and
        the card records the one it replaces (section 7). A line whose name (new or old)
        is the name or alias of a household item by now is refused, so confirming does
        not add a second item, and so are two lines with the same name. Raises
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
        before = [_new_item_line(op) for op in old.operations]
        after = [
            _with_new_item_change(line, changed.get(op.operation_id))
            for line, op in zip(before, old.operations, strict=True)
        ]
        if after == before:
            raise CardChangeRefused("nothing_changed", CHANGE_NOTHING)
        refused = []
        names = [normalize_term(line.name) for line in after]
        for line, op, key in zip(after, old.operations, names, strict=True):
            same = match_term(line.name, list(household_items))
            if same.items:
                have = "、".join(i.name for i in same.items)
                refused.append((op, CHANGE_PHOTO_HAVE_NOW.format(names=have)))
            elif names.count(key) > 1:
                refused.append((op, CHANGE_SAME_NAME_TWICE.format(name=line.name)))
        if refused:
            _refuse_lines(refused)
        # Each line keeps its unit and source wording (photo or said, Issue #168).
        lines = [_new_item_again(op, line) for line, op in zip(after, old.operations, strict=True)]
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
    candidate_item_id: str | None = None


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
        self,
        household_id: str,
        args: dict[str, Any],
        *,
        replaces_approval_id: str | None = None,
        photo: CardPhoto | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """(result for the model, card for the App). Writes nothing but the pending card.

        `replaces_approval_id` is the card a 送出修改 replaces; the new card records it
        (section 7). `photo` is the photo sent with the message: a card with exactly one
        line keeps it (Issue #176), and the model is told whether it is kept (`photo`).
        """
        requested = _requested_items(args.get("items"), dated=self._dated)
        if not requested:
            return json.dumps({"error": "no_items"}), None
        lines, matches, left_out = await self._lines(household_id, requested)
        if matches and not lines:
            # Issue #175: an item-match card holds one item; any others are said again.
            first, *later = matches
            left_out.extend(_said_later(match) for match in later)
            note = None if photo is None else PHOTO_NOT_ON_MATCH
            return await self._propose_match(
                household_id, first, left_out, replaces_approval_id, note
            )
        left_out.extend(_said_later(match) for match in matches)
        if not lines:
            return json.dumps({"card": None, "not_on_card": left_out}, ensure_ascii=False), None
        photo_note = None
        if photo is not None:
            lines, photo_note = await self._with_photo(household_id, lines, photo)
        record, error = await self._store(household_id, lines, replaces_approval_id)
        if record is None:
            return error, None
        card = ApprovalService.to_pending_approval(record)
        view: dict[str, Any] = {
            "card": {"lines": [line.description for line in lines]},
            "not_on_card": left_out,
        }
        if photo_note is not None:
            view["photo"] = photo_note
        return json.dumps(view, ensure_ascii=False), card.model_dump(mode="json")

    async def _with_photo(
        self, household_id: str, lines: list[NewOperation], photo: CardPhoto
    ) -> tuple[list[NewOperation], str | None]:
        """(the lines, with the photo on the one line when it can be kept; the model's note).

        Issue #176: only a card with exactly one line keeps the photo, and never over an
        inventory item's own photo. The item is read again for its photo; if it changed
        since the line was made, the line is left as it is and storing the card reports
        the change (`RECORDS_CHANGED`).
        """
        if not photo.ready:
            return lines, PHOTO_NOT_READY
        if len(lines) != 1:
            return lines, PHOTO_ONE_LINE_ONLY
        line = lines[0]
        if line.action == COMPLETE_PURCHASE:
            inventory = await self._reader.read_inventory_item_with_etag(
                household_id=household_id, item_id=str(line.values["item_id"])
            )
            shown = {target.record_id: target.etag for target in line.targets}
            if inventory is None or shown.get(inventory.document_id) != inventory.etag:
                return lines, None
            if inventory.record.image_ref is not None:
                return lines, PHOTO_ITEM_HAS_ONE.format(name=inventory.record.name)
        return [with_photo(line, photo)], PHOTO_KEPT

    async def _propose_match(
        self,
        household_id: str,
        line: NewOperation,
        left_out: list[dict[str, Any]],
        replaces_approval_id: str | None,
        photo_note: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Store the item-match card for one unsure item (contract section 10)."""
        candidate = str(line.values["candidate_name"])
        record, error = await self._store(
            household_id, [line], replaces_approval_id, options=item_match_options(candidate)
        )
        if record is None:
            return error, None
        card = ApprovalService.to_pending_approval(record)
        view = {
            "card": {"item_match": line.description},
            "not_on_card": left_out,
            "note": ITEM_MATCH_MADE.format(term=line.values["user_term"], candidate=candidate),
        }
        if photo_note is not None:
            view["photo"] = photo_note
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

        A photo on `old` stays on the new card (Issue #176), with the values it had,
        unless the item has a photo of its own by now; there is no model reply to say so.
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
        # Name and place change only on a photo or said new-item card (Issue #188).
        renamed = [
            by_id[c.operation_id] for c in changes if {"name", "location"} & c.model_fields_set
        ]
        if renamed:
            _refuse_lines([(op, CHANGE_NO_NAME_OR_PLACE) for op in renamed])
        if not self._dated and any("purchased_on" in item for item in before if item):
            raise CardChangeRefused("purchase_day_off", CHANGE_DAY_OFF)
        after = [
            _with_change(item, changed.get(op.operation_id), today)
            for op, item in zip(old.operations, before, strict=True)
            if item is not None
        ]
        if after == before:
            raise CardChangeRefused("nothing_changed", CHANGE_NOTHING)
        lines, matches, left_out = await self._lines(
            old.household_id, _requested_items(after, dated=self._dated)
        )
        # A list entry the household has by now is not made an item-match card here: the
        # changed card keeps the lines it had (Issue #175 makes item-match cards one item).
        left_out.extend(
            {
                "shopping_item_id": match.values["shopping_item_id"],
                "reason": ALREADY_HAVE.format(names=match.values["candidate_name"]),
            }
            for match in matches
        )
        if left_out:
            by_entry = {str(op.values.get("shopping_item_id")): op for op in old.operations}
            _refuse_lines([(by_entry[row["shopping_item_id"]], row["reason"]) for row in left_out])
        kept = [photo for op in old.operations if (photo := CardPhoto.on_line(op)) is not None]
        if kept:
            lines, _ = await self._with_photo(old.household_id, lines, kept[0])
        record, error = await self._store(old.household_id, lines, old.approval_id)
        if record is None:
            if error == CARD_FAILED:
                raise RuntimeError("The changed card was not stored.")
            raise CardChangeRefused("line_refused", CHANGE_LINE_REFUSED + CHANGE_RECORDS_MOVED)
        return ApprovalService.to_pending_approval(record).model_dump(mode="json")

    async def _lines(
        self, household_id: str, requested: Sequence[_Requested]
    ) -> tuple[list[NewOperation], list[NewOperation], list[dict[str, Any]]]:
        """(card lines, item-match lines, the items left off with why).

        An item-match line (`confirm_item_match`) is for an item that may be a household
        item under another name (Issue #175); it never shares a card with other lines.
        """
        lines: list[NewOperation] = []
        matches: list[NewOperation] = []
        left_out: list[dict[str, Any]] = []
        for item in requested:
            line, reason = await self._line(household_id, item)
            if line is None:
                left_out.append({"shopping_item_id": item.shopping_item_id, "reason": reason})
            elif line.action == CONFIRM_ITEM_MATCH:
                matches.append(line)
            else:
                lines.append(line)
        return lines, matches, left_out

    async def _store(
        self,
        household_id: str,
        lines: list[NewOperation],
        replaces_approval_id: str | None,
        *,
        options: Sequence[NewOption] = (),
    ) -> tuple[ApprovalRecord | None, str]:
        """Store the pending card: (record, "") or (None, the error for the model).

        With `options` it is an item-match card (one line, 「是」 and 「不是」).
        """
        if options:
            presentation = ApprovalPresentation.ITEM_MATCH
        elif len(lines) == 1:
            presentation = ApprovalPresentation.SINGLE_ACTION
        else:
            presentation = ApprovalPresentation.BATCH_ACTION
        try:
            record = await self._approvals.save(
                self._approvals.prepare(
                    household_id=household_id,
                    presentation=presentation,
                    operations=lines,
                    options=options,
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
        """A list entry not linked to an inventory item: a new item, or maybe a household one.

        It may be a household item when the model names one (`candidate_item_id`) or when
        exactly one household item has the entry's name or alias; the line is then an
        item-match line (Issue #175). Several such items cannot be told apart, so no line
        is made. Otherwise it becomes a `create_item` line.
        """
        entry = shopping.record
        if item.candidate_item_id is not None:
            return await self._match_line(
                household_id, shopping, item, item.candidate_item_id, NO_SUCH_ITEM, purchased_at
            )
        same = match_term(entry.name, await self._reader.list_inventory(household_id))
        if len(same.items) == 1:
            already = ALREADY_HAVE.format(names=same.items[0].name)
            return await self._match_line(
                household_id, shopping, item, same.items[0].item_id, already, purchased_at
            )
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

    async def _match_line(
        self,
        household_id: str,
        shopping: MappedDocument[ShoppingItem],
        item: _Requested,
        candidate_item_id: str,
        missing: str,
        purchased_at: datetime | None,
    ) -> tuple[NewOperation | None, str]:
        """The item-match line for one unsure item (contract section 10, Issue #175)."""
        candidate = await self._reader.read_inventory_item_with_etag(
            household_id=household_id, item_id=candidate_item_id
        )
        if candidate is None:
            return None, missing
        listed_unit = shopping.record.unit
        if (listed_unit is not None and listed_unit != candidate.record.unit) or (
            item.unit is not None and item.unit != candidate.record.unit
        ):
            return None, OTHER_UNIT
        try:
            line = item_match_operation(
                shopping=shopping,
                inventory=candidate,
                quantity=item.quantity,
                purchased_at=purchased_at,
            )
        except (ShoppingPreviewError, ValueError) as error:
            return None, f"這一筆現在還不能記錄：{error}"
        return line, ""


def _said_later(match: NewOperation) -> dict[str, Any]:
    """An unsure item left off a card with other items: said again afterwards (#175)."""
    return {
        "shopping_item_id": match.values["shopping_item_id"],
        "reason": MATCH_LATER.format(candidate=match.values["candidate_name"]),
    }


def _not_replaced_because(record: ApprovalRecord | None) -> str | None:
    """None when the old card is cancelled as `replaced`; otherwise why it is not."""
    if record is None or record.status is ApprovalStatus.PENDING:
        return STILL_OPEN
    if record.is_replaced:
        return None
    return record.status.value


def _requested_items(value: Any, *, dated: bool = False) -> list[_Requested]:
    """The model's items, each shopping entry once, quantities as whole numbers ≥ 1.

    `unit`, `location` and `candidate_item_id` are optional words, and so is
    `purchased_on`, which is read only with dated purchases on. Blank text counts as
    not said: the real model fills unused optional fields with "" (seen on Azure
    OpenAI, 2026-11-13).
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
        words = [item.get("unit"), item.get("location"), day, item.get("candidate_item_id")]
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
        unit, location, purchased_on, candidate = ((w or "").strip() or None for w in words)
        requested.append(
            _Requested(shopping_item_id, quantity, unit, location, purchased_on, candidate)
        )
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
    """What the App shows after the user answers a card. "Done" only when written (#52).

    An item-match card answered 「不是」 is closed with nothing written (section 10): the
    reply says so and asks for the item again with where it goes, unless the household
    already has an item of that name, which is never added twice.
    """
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
    if (
        result.outcome is ResumeOutcome.CANCELLED
        and result.record is not None
        and result.record.cancel_reason is CancelReason.NOT_SAME_ITEM
    ):
        message = _not_same_item_message(result.record)
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


def _not_same_item_message(record: ApprovalRecord) -> str:
    """The reply to 「不是」 on an item-match card (Issue #175)."""
    values = record.operations[0].values
    term = str(values.get("user_term"))
    candidate = str(values.get("candidate_name"))
    if normalize_term(term) == normalize_term(candidate):
        return NOT_SAME_ITEM_SAME_NAME.format(candidate=candidate)
    return NOT_SAME_ITEM_REPLY.format(
        term=term,
        candidate=candidate,
        quantity=_number(values.get("quantity")),
        unit=values.get("unit"),
    )


def _written_lines(record: ApprovalRecord) -> Sequence[str]:
    outcome = record.outcome or {}
    lines = []
    for operation in record.operations:
        written = outcome.get(operation.operation_id)
        if not isinstance(written, dict):
            continue
        # An item-match line is written into the household item it asked about.
        name = operation.values.get("name") or operation.values.get("candidate_name")
        line = f"{name} +{_number(written.get('quantity_added'))}"
        line += f" {written.get('unit')}（現在 {_number(written.get('inventory_quantity'))}"
        line += f" {written.get('unit')}）"
        lines.append(line)
    return lines


def _number(value: Any) -> Any:
    return int(value) if isinstance(value, float) and value.is_integer() else value
