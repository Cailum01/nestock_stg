"""A1: which builder plans the writes for a confirmed card (`/agent/resume`, Issue #124).

`ApprovalService` takes one `DomainWriteBuilder`. A1's cards are routed by action and presentation:

- Purchase cards (v8 flow C, contract section 11) go to C1's `ShoppingWriteBuilder`,
  as before.
- No-Shopping Inventory stock cards route to B1's `PhotoItemWriteBuilder`. `create_item`
  lines use C1's `plan_adjustment`; `increase_stock` lines use C1's existing-item plan
  and require the card-bound Inventory ETag and quantity snapshot (Issue #206). Both
  can share one `batch_action`; B1 commits each line's Transaction and ChangeEvent,
  Inventory changes, receipts and approval completion in one household batch. New-item
  lines are also checked against current household Inventory before writing.
- Item-match cards (contract section 10, Issue #175) answered 「是」 are a purchase of
  the list entry into the matched household item. They go to C1's builder, which
  checks the line against the card's shown and fixed values (PR #183); A1's
  `CardInputResolver` reads the line's `candidate_item_id` as the purchase target.
  「不是」 never reaches a builder: the approval service closes the card without
  writing.

B1 puts this router in `get_approval_service` (`app/core/dependencies.py`).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.agent import ApprovalPresentation
from app.models.domain import InventoryItem
from app.services.approvals import (
    SAME_ITEM,
    ApprovalRecord,
    DomainWriteBuilder,
    DomainWritePlan,
    TargetChanged,
)
from app.services.cosmos_read import MappedDocument
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.item_matching import match_term
from app.services.photo_item_writes import INCREASE_STOCK, ExistingInventoryReader
from app.services.purchase_inputs import CREATE_ITEM, UnsupportedAction

PHOTO_WRITE_MISSING = "Photo new-item cards cannot be written yet (Issue #124)."
INCREASE_STOCK_WRITE_MISSING = "Existing-item stock cards cannot be written yet (Issue #206)."
NOT_SAME_ITEM_WRITES_NOTHING = "An item-match card answered 「不是」 writes nothing (section 10)."


class HouseholdItems(Protocol):
    """Reads a household's inventory items (B1's `CosmosReadRepository`)."""

    async def list_inventory(self, household_id: str) -> list[InventoryItem]: ...

    async def read_inventory_item_with_etag(
        self, *, household_id: str, item_id: str
    ) -> MappedDocument[InventoryItem] | None: ...


@runtime_checkable
class StockCardBuilder(Protocol):
    async def build_stock_card(
        self,
        record: ApprovalRecord,
        inventory_reader: ExistingInventoryReader | None,
    ) -> DomainWritePlan: ...


class CardWriteRouter:
    """A `DomainWriteBuilder` that hands each card to the builder that writes it."""

    def __init__(
        self,
        purchases: DomainWriteBuilder,
        photo_items: DomainWriteBuilder | None = None,
        household_items: HouseholdItems | None = None,
    ) -> None:
        self._purchases = purchases
        self._photo_items = photo_items
        self._household_items = household_items

    async def build(self, record: ApprovalRecord) -> DomainWritePlan:
        if record.presentation is ApprovalPresentation.ITEM_MATCH:
            if record.selected_option_id != SAME_ITEM:
                raise UnsupportedAction(NOT_SAME_ITEM_WRITES_NOTHING)
            return await self._purchases.build(record)
        if _is_stock_addition_card(record):
            if self._photo_items is None:
                message = (
                    INCREASE_STOCK_WRITE_MISSING
                    if any(operation.action == INCREASE_STOCK for operation in record.operations)
                    else PHOTO_WRITE_MISSING
                )
                raise UnsupportedAction(message)
            await self._check_still_new(record)
            if any(operation.action == INCREASE_STOCK for operation in record.operations):
                if not isinstance(self._photo_items, StockCardBuilder):
                    raise UnsupportedAction(
                        "The configured Inventory writer does not support increase_stock."
                    )
                return await self._photo_items.build_stock_card(record, self._household_items)
            return await self._photo_items.build(record)
        return await self._purchases.build(record)

    async def _check_still_new(self, record: ApprovalRecord) -> None:
        """Stop the write when a new-item line is a household item by now (PR #187).

        The card was checked when it was made; another write may have added an item of
        the same name or alias since. The household's items are read again: an approved
        line whose name is the name or alias of a household item (#36 layers 1 and 2,
        `match_term`, as when the card was made) raises `TargetChanged`. Nothing is
        written, the card is cancelled and the user is asked to say it again
        (`needs_new_card`). The item a line creates itself does not count, so a retry
        is not refused. The read and the batch are not one step: an item written in
        between is not caught (contract section 10). Without a reader nothing is read.
        """
        if self._household_items is None:
            return
        approved = set(record.approved_operation_ids or ())
        lines = [
            op
            for op in record.operations
            if op.operation_id in approved and op.action == CREATE_ITEM
        ]
        if not lines:
            return
        items = await self._household_items.list_inventory(record.household_id)
        for operation in lines:
            own = resolve_inventory_item_id(operation.operation_id)
            others = [item for item in items if item.item_id != own]
            if match_term(str(operation.values.get("name") or ""), others).items:
                raise TargetChanged(f"{operation.operation_id}: the household has it by now.")


def _is_stock_addition_card(record: ApprovalRecord) -> bool:
    return bool(record.operations) and all(
        operation.action in (CREATE_ITEM, INCREASE_STOCK)
        and not operation.values.get("shopping_item_id")
        for operation in record.operations
    )
