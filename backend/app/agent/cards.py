"""A1: confirmation cards the Agent makes, and the reply after the App answers one.

v8 flow C (買回來了): the `complete_purchase` tool writes nothing. It reads the shopping
entries and the inventory items they add to, with their versions, makes one card line
per bought item (C1's preview checks each, `services/purchase_writes.py`), and stores
a pending card through the approval service. The App shows the card; the write
happens only in `POST /agent/resume` after the user confirms
(`contracts/approval-lifecycle.md`).

Not covered yet: items that are not on the list or not linked to an inventory item
(Issue #90), buying more than listed, a different unit, a purchase time other than
the confirmation time, spreading more items than one batch holds over several cards,
changing a card (Issue #91), and reminding the user of open cards.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from app.models.agent import AgentResponse, AgentResult, ApprovalPresentation, ResultKind
from app.models.domain import ShoppingStatus
from app.services.approvals import (
    ApprovalRecord,
    ApprovalService,
    BatchTooLarge,
    NewOperation,
    ResumeOutcome,
    ResumeResult,
    TargetChanged,
)
from app.services.purchase_writes import (
    COMPLETE_PURCHASE,
    PurchaseRecordReader,
    purchase_operation,
)
from app.services.shopping.preview import ShoppingPreviewError

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
                                "description": "使用者說買了幾個，用待購清單上的單位。",
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
ONE_CARD_PER_MESSAGE = json.dumps({"error": "card_already_made"})
"""A second `complete_purchase` in the same message: the first card stands."""
CARD_FAILED = json.dumps({"error": "card_not_made"})
TOO_MANY_ITEMS = json.dumps({"error": "too_many_items"})
RECORDS_CHANGED = json.dumps({"error": "records_changed"})


class PurchaseCards:
    """The `complete_purchase` tool: makes and stores a pending purchase card."""

    def __init__(self, reader: PurchaseRecordReader, approvals: ApprovalService) -> None:
        self._reader = reader
        self._approvals = approvals

    async def propose(
        self, household_id: str, args: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """(result for the model, card for the App). Writes nothing but the pending card."""
        requested = _requested_items(args.get("items"))
        if not requested:
            return json.dumps({"error": "no_items"}), None
        lines: list[NewOperation] = []
        left_out: list[dict[str, Any]] = []
        for shopping_item_id, quantity in requested:
            line, reason = await self._line(household_id, shopping_item_id, quantity)
            if line is None:
                left_out.append({"shopping_item_id": shopping_item_id, "reason": reason})
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

    async def _line(
        self, household_id: str, shopping_item_id: str, quantity: int
    ) -> tuple[NewOperation | None, str]:
        shopping = await self._reader.read_shopping_item_with_etag(
            household_id=household_id, shopping_item_id=shopping_item_id
        )
        if shopping is None or shopping.record.status is not ShoppingStatus.PENDING:
            return None, NOT_ON_LIST
        entry = shopping.record
        if not entry.item_id:
            return None, NOT_LINKED
        if quantity > entry.quantity:
            return None, MORE_THAN_LISTED
        inventory = await self._reader.read_inventory_item_with_etag(
            household_id=household_id, item_id=entry.item_id
        )
        if inventory is None:
            return None, NOT_LINKED
        if inventory.record.unit != entry.unit:
            return None, OTHER_UNIT
        try:
            return purchase_operation(shopping=shopping, inventory=inventory, quantity=quantity), ""
        except (ShoppingPreviewError, ValueError) as error:
            return None, f"這一筆現在還不能記錄：{error}"


def _requested_items(value: Any) -> list[tuple[str, int]]:
    """The model's items, each shopping entry once, quantities as whole numbers ≥ 1."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    requested: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        shopping_item_id = str(item.get("shopping_item_id") or "").strip()
        quantity = item.get("quantity")
        if (
            not shopping_item_id
            or shopping_item_id in seen
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 1
        ):
            return []
        seen.add(shopping_item_id)
        requested.append((shopping_item_id, quantity))
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
        line = f"{operation.values.get('name')} +{operation.values.get('purchased_quantity')}"
        line += f" {written.get('unit')}（現在 {_number(written.get('inventory_quantity'))}"
        line += f" {written.get('unit')}）"
        lines.append(line)
    return lines


def _number(value: Any) -> Any:
    return int(value) if isinstance(value, float) and value.is_integer() else value
