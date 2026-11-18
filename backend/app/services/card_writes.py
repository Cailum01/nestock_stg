"""A1: which builder plans the writes for a confirmed card (`/agent/resume`, Issue #124).

`ApprovalService` takes one `DomainWriteBuilder`. A1's cards are of three kinds:

- Purchase cards (v8 flow C, contract section 11) go to C1's `ShoppingWriteBuilder`,
  as before.
- Photo new-item cards (contract section 10) have no Shopping List entry. They go to
  B1's builder for a new item without one (Issue #124): it takes the lines from A1's
  `PhotoInputResolver`, plans each with C1's `plan_adjustment` and writes the item,
  the `adjustment` Transaction, the change event and the receipts with the card in
  one batch: `PhotoItemWriteBuilder` (PR #171). Without it, a photo card is refused
  here and nothing is written. New-item cards the user said (`add_stock`, Issue #168)
  take the same path. Before it, the household's items are read again (PR #187 B1
  review): a line the household has by now stops the write.
- Item-match cards (contract section 10, Issue #175) answered 「是」 are a purchase of
  the list entry into the matched household item. They go to C1's builder, which
  checks the line against the card's shown and fixed values (PR #183); A1's
  `CardInputResolver` reads the line's `candidate_item_id` as the purchase target.
  「不是」 never reaches a builder: the approval service closes the card without
  writing.

- A card with a line that adds to an item the household has, with no Shopping List
  entry (`increase_stock`, Issue #206, contract section 10.1), goes whole to the same
  no-Shopping builder as new-item cards, through its `build_stock_card`, with the
  reader for the item's current version: new-item lines on the same card are written
  with it in one batch. The builder is B1's (PR #208) with C1's existing-item plan;
  while the configured builder has no `build_stock_card`, the card is refused here and
  nothing is written, so `ApprovalService.save` stores no such card. It never goes to
  C1's purchase builder.

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
from app.services.purchase_inputs import (
    CREATE_ITEM,
    INCREASE_STOCK,
    UnsupportedAction,
    has_stock_increase,
)

PHOTO_WRITE_MISSING = "Photo new-item cards cannot be written yet (Issue #124)."
INCREASE_STOCK_WRITE_MISSING = "Existing-item stock cards cannot be written yet (Issue #206)."
INCREASE_STOCK_NOT_SUPPORTED = "The configured Inventory writer does not support increase_stock."
NOT_SAME_ITEM_WRITES_NOTHING = "An item-match card answered 「不是」 writes nothing (section 10)."


class HouseholdItems(Protocol):
    """Reads a household's inventory items (B1's `CosmosReadRepository`)."""

    async def list_inventory(self, household_id: str) -> list[InventoryItem]: ...

    async def read_inventory_item_with_etag(
        self, *, household_id: str, item_id: str
    ) -> MappedDocument[InventoryItem] | None: ...


@runtime_checkable
class StockCardBuilder(Protocol):
    """A no-Shopping builder that also writes `increase_stock` lines (B1, PR #208).

    It reads each item again through `inventory_reader` and checks it against the
    version and quantity the card holds before planning the write.
    """

    async def build_stock_card(
        self,
        record: ApprovalRecord,
        inventory_reader: HouseholdItems | None,
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
        increases = has_stock_increase(record)
        if _is_stock_addition_card(record):
            if self._photo_items is None:
                raise UnsupportedAction(
                    INCREASE_STOCK_WRITE_MISSING if increases else PHOTO_WRITE_MISSING
                )
            await self._check_still_new(record)
            if increases:
                if not isinstance(self._photo_items, StockCardBuilder):
                    raise UnsupportedAction(INCREASE_STOCK_NOT_SUPPORTED)
                return await self._photo_items.build_stock_card(record, self._household_items)
            return await self._photo_items.build(record)
        if increases:
            # Never C1's purchase builder: there is no Shopping List entry to complete.
            raise UnsupportedAction(INCREASE_STOCK_WRITE_MISSING)
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
    """Only `create_item` and `increase_stock` lines, none from the Shopping List."""
    return bool(record.operations) and all(
        operation.action in (CREATE_ITEM, INCREASE_STOCK)
        and not operation.values.get("shopping_item_id")
        for operation in record.operations
    )
