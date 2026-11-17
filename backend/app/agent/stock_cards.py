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

An item whose name or alias the household already has gets no line (Issue #168 point
3), so confirming never adds a second item; adding to an item the household has is
not covered. When the model is unsure whether it is a household item under another
name, the prompt tells it to ask the user before calling the tool. The quantity can
be changed on the card like a photo card's (`PhotoCards.change_card`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.cards import CARD_FAILED, TOO_MANY_ITEMS, said_item_operation
from app.models.agent import ApprovalPresentation
from app.models.domain import InventoryItem
from app.services.approvals import ApprovalService, BatchTooLarge
from app.services.item_matching import match_term, normalize_term

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


class StockRecordReader(Protocol):
    """B1's `CosmosReadRepository`: the household's items, to find names it has."""

    async def list_inventory(self, household_id: str) -> list[InventoryItem]: ...


@dataclass(frozen=True)
class _Said:
    name: str
    quantity: int
    unit: str
    location: str | None


class StockCards:
    """The `add_stock` tool: makes and stores one pending new-item card (Issue #168)."""

    def __init__(self, reader: StockRecordReader, approvals: ApprovalService) -> None:
        self._reader = reader
        self._approvals = approvals

    @property
    def tool_schema(self) -> dict[str, Any]:
        return STOCK_TOOL_SCHEMA

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
        for item in said:
            key = normalize_term(item.name)
            if key in seen:
                left_out.append({"name": item.name, "reason": SAID_TWICE})
                continue
            seen.add(key)
            have = match_term(item.name, household)
            if have.items:
                names = "、".join(i.name for i in have.items)
                left_out.append({"name": item.name, "reason": HAVE_IT.format(names=names)})
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
            "note": STOCK_CARD_MADE,
        }
        card = ApprovalService.to_pending_approval(record).model_dump(mode="json")
        return json.dumps(view, ensure_ascii=False), card


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
