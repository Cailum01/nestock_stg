"""A1: the `add_stock` tool, new-item cards for things the user says they got (Issue #168).

「我收到一瓶洗髮精」: something new came home that is not a Shopping List entry (a gift,
something received, or bought off the list). Like `complete_purchase`, the tool
writes nothing: it stores one pending card and returns it.

The card is the photo new-item card's format (contract section 10): `create_item`
lines with `name`, `quantity`, `unit`, `location` (the place the user said, or None
and the App shows 未填) and `transaction_id`, no `shopping_item_id` and no target
versions; the description ends with （使用者說的）. Confirming goes the photo way:
A1's `CardWriteRouter` sends it to B1's `PhotoItemWriteBuilder`, which plans each line
with C1's `plan_adjustment` (reason `adjustment`, agreed by C1 and B2 in Issue #168)
and writes the item, the Transaction, the change event and the receipts in one batch.

An item whose name or alias the household already has gets no new-item line (Issue
#168 point 3), so confirming never adds a second item. When the model is unsure
whether it is a household item under another name, the prompt tells it to ask the
user before calling the tool. The quantity can be changed on the card like a photo
card's (`PhotoCards.change_card`).

Adding to an item the household has (Issue #206) is `StockIncreases`: with it, such
an item gets an `increase_stock` line on the same card instead of being left out.
It is on in the running app unless `NESTOCK_STOCK_INCREASE=0`
(`app/agent/read_agent.py`); a photo uses the same lines.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.cards import (
    CARD_FAILED,
    SAID,
    TOO_MANY_ITEMS,
    said_item_operation,
    stock_increase_operation,
)
from app.models.agent import ApprovalPresentation
from app.models.domain import InventoryItem
from app.services.approvals import ApprovalService, BatchTooLarge, NewOperation
from app.services.cosmos_read import MappedDocument
from app.services.item_matching import ItemMatch, match_term, normalize_term
from app.services.purchase_inputs import INCREASE_STOCK

logger = logging.getLogger(__name__)

ADD_STOCK = "add_stock"
"""v8's tool name for adding stock that does not come from the Shopping List."""

STOCK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ADD_STOCK,
        "description": (
            "使用者說家裡多了某樣家裡還沒有的東西，而且不是待購清單上買回來的"
            "（例如朋友送的、收到的、在待購清單以外買的）時呼叫。"
            "只會出一張新增庫存品項的確認卡；使用者按確認之前，不會改庫存。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "這次多了的東西，一樣一項。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "使用者說的品名。"},
                            "quantity": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "使用者說有幾個。",
                            },
                            "unit": {
                                "type": "string",
                                "description": "使用者說的單位，例如瓶、包、顆。",
                            },
                            "location": {
                                "type": "string",
                                "description": "使用者說要放在哪裡；沒說就不要填。",
                            },
                        },
                        "required": ["name", "quantity", "unit"],
                    },
                }
            },
            "required": ["items"],
        },
    },
}

HAVE_IT = "家裡已經有「{names}」；用說的只能新增家裡還沒有的東西，這一樣不會出卡。"
SAID_TWICE = "同一樣東西說了兩次，只算第一次。"
STOCK_CARD_MADE = (
    "已出一張新增庫存品項的確認卡，還沒有記錄。請使用者確認或取消；沒說放在哪裡的，卡上會顯示未填。"
)

# Adding to an item the household has (Issue #206); only with `StockIncreases`.
STOCK_INCREASE_VAR = "NESTOCK_STOCK_INCREASE"
"""`NESTOCK_STOCK_INCREASE=0` turns this off. Unset, it is on (Issue #206)."""
INCREASE_TOOL_DESCRIPTION = (
    "使用者說家裡多了某樣東西，而且不是待購清單上買回來的"
    "（例如朋友送的、收到的、在待購清單以外買的）時呼叫，家裡已經有這樣東西也可以。"
    "家裡還沒有的會新增品項，家裡已經有的會加數量。"
    "只會出一張確認卡；使用者按確認之前，不會改庫存。"
)
INCREASE_PROMPT_NOTE = (
    "【家裡已有的品項也能加數量】「家裡多了東西」一節第 2 點改成："
    "家裡已經有同名或同別名的，也呼叫 `add_stock`，`name` 填家裡那個品項的名稱，"
    "`unit` 填家裡那個品項的單位；卡上會是幫那個品項加數量，不會新增第二個品項。"
    "卡片還沒按確認之前，一樣不要說已經記錄。"
)
INCREASE_CARD_NOTE = "卡上有幫家裡已有的品項加數量的項目，還沒有記錄。請使用者確認或取消。"
WHICH_ONE = "家裡有好幾樣都叫「{names}」，請使用者說是哪一樣。"
UNIT_DIFFERS = (
    "家裡的「{name}」單位是{unit}，跟說的{said}不同；請使用者用{unit}再說一次有幾{unit}。"
)
ITEM_GONE = "家裡的「{name}」剛剛被改過或不見了，請使用者再說一次。"


class StockRecordReader(Protocol):
    """B1's `CosmosReadRepository`: the household's items, to find names it has."""

    async def list_inventory(self, household_id: str) -> list[InventoryItem]: ...


@dataclass(frozen=True)
class _Said:
    name: str
    quantity: int
    unit: str
    location: str | None


class ItemVersionReader(Protocol):
    """B1's `CosmosReadRepository`: one household item with its ETag."""

    async def read_inventory_item_with_etag(
        self, *, household_id: str, item_id: str
    ) -> MappedDocument[InventoryItem] | None: ...


class StockIncreases:
    """Card lines that add to an item the household has, with no list entry (Issue #206).

    Used by `StockCards` (the user said it) and by the photo path (`ReadAgent`). The
    item is read again with its ETag when the line is made, so the card holds the
    version the user saw; nothing is written.
    """

    def __init__(self, reader: ItemVersionReader) -> None:
        self._reader = reader

    async def line(
        self, household_id: str, match: ItemMatch, *, quantity: int, unit: str, source: str
    ) -> tuple[NewOperation | None, str | None]:
        """(the line, None) or (None, why there is no line, for the model to say).

        One line needs exactly one household item of that name or alias, in the
        item's own unit: a different unit is asked again, never converted.
        """
        if match.ambiguous:
            return None, WHICH_ONE.format(names="、".join(i.name for i in match.items))
        item = match.matched
        if item is None:
            raise ValueError("The household has no such item; this is a new-item line.")
        if normalize_term(unit) != normalize_term(item.unit):
            return None, UNIT_DIFFERS.format(name=item.name, unit=item.unit, said=unit)
        current = await self._reader.read_inventory_item_with_etag(
            household_id=household_id, item_id=item.item_id
        )
        if current is None or current.etag is None:
            return None, ITEM_GONE.format(name=item.name)
        return stock_increase_operation(inventory=current, quantity=quantity, source=source), None


def _with_description(schema: dict[str, Any], description: str) -> dict[str, Any]:
    function = {**schema["function"], "description": description}
    return {**schema, "function": function}


INCREASE_TOOL_SCHEMA = _with_description(STOCK_TOOL_SCHEMA, INCREASE_TOOL_DESCRIPTION)


class StockCards:
    """The `add_stock` tool: makes and stores one pending card (Issue #168).

    New-item lines for things the household does not have. With `increases`, a thing
    the household has gets a line that adds to it (Issue #206); without, it is left out.
    """

    def __init__(
        self,
        reader: StockRecordReader,
        approvals: ApprovalService,
        increases: StockIncreases | None = None,
    ) -> None:
        self._reader = reader
        self._approvals = approvals
        self._increases = increases

    @property
    def tool_schema(self) -> dict[str, Any]:
        return STOCK_TOOL_SCHEMA if self._increases is None else INCREASE_TOOL_SCHEMA

    def prompt_note(self) -> str | None:
        """What the system prompt gets while adding to household items is on."""
        return None if self._increases is None else INCREASE_PROMPT_NOTE

    async def propose(
        self, household_id: str, args: dict[str, Any], *, replaces_approval_id: str | None = None
    ) -> tuple[str, dict[str, Any] | None]:
        """(result for the model, card for the App). Writes nothing but the pending card.

        `replaces_approval_id` is the card a typed change replaces (section 7).
        """
        said = _said_items(args.get("items"))
        if not said:
            return json.dumps({"error": "no_items"}), None
        household = await self._reader.list_inventory(household_id)
        lines, left_out, seen = [], [], set()
        adding_to: set[str] = set()
        for item in said:
            key = normalize_term(item.name)
            if key in seen:
                left_out.append({"name": item.name, "reason": SAID_TWICE})
                continue
            seen.add(key)
            have = match_term(item.name, household)
            if have.items and self._increases is None:
                names = "、".join(i.name for i in have.items)
                left_out.append({"name": item.name, "reason": HAVE_IT.format(names=names)})
                continue
            if have.items:
                line, why = await self._increases.line(
                    household_id, have, quantity=item.quantity, unit=item.unit, source=SAID
                )
                if line is None:
                    left_out.append({"name": item.name, "reason": why})
                    continue
                if line.values["item_id"] in adding_to:
                    left_out.append({"name": item.name, "reason": SAID_TWICE})
                    continue
                adding_to.add(line.values["item_id"])
                lines.append(line)
                continue
            lines.append(
                said_item_operation(
                    name=item.name, quantity=item.quantity, unit=item.unit, location=item.location
                )
            )
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
        except Exception:  # storage or planning failed; no card was stored
            logger.warning("Said new-item card was not stored", exc_info=True)
            return CARD_FAILED, None
        view = {
            "card": {"lines": [line.description for line in lines]},
            "not_on_card": left_out,
            "note": _card_note(lines),
        }
        card = ApprovalService.to_pending_approval(record).model_dump(mode="json")
        return json.dumps(view, ensure_ascii=False), card


def _card_note(lines: Sequence[NewOperation]) -> str:
    """What the model is told about the card: new items, added-to items, or both."""
    increases = [line for line in lines if line.action == INCREASE_STOCK]
    if not increases:
        return STOCK_CARD_MADE
    if len(increases) == len(lines):
        return INCREASE_CARD_NOTE
    return STOCK_CARD_MADE + INCREASE_CARD_NOTE


def _said_items(value: Any) -> Sequence[_Said]:
    """The model's items: a name, a whole quantity ≥ 1 and a unit; location optional.

    Blank text counts as not said (the real model fills unused optional fields with
    ""). Anything unusable makes no card at all.
    """
    if not isinstance(value, list):
        return []
    said: list[_Said] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        quantity = item.get("quantity")
        words = [item.get("name"), item.get("unit"), item.get("location")]
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 1
            or any(w is not None and not isinstance(w, str) for w in words)
        ):
            return []
        name, unit, location = ((w or "").strip() or None for w in words)
        if name is None or unit is None:
            return []
        said.append(_Said(name, quantity, unit, location))
    return said
