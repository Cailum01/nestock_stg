"""A1: which builder plans the writes for a confirmed card (`/agent/resume`, Issue #124).

`ApprovalService` takes one `DomainWriteBuilder`. A1's cards are of three kinds:

- Purchase cards (v8 flow C, contract section 11) go to C1's `ShoppingWriteBuilder`,
  as before.
- Photo new-item cards (contract section 10) have no Shopping List entry. They go to
  B1's builder for a new item without one (Issue #124): it takes the lines from A1's
  `PhotoInputResolver`, plans each with C1's `plan_adjustment` and writes the item,
  the `adjustment` Transaction, the change event and the receipts with the card in
  one batch: `PhotoItemWriteBuilder` (PR #171). Without it, a photo card is refused
  here and nothing is written.
- Item-match cards (contract section 10, Issue #175) answered 「是」 are a purchase of
  the list entry into the matched household item. They go to C1's builder as a
  one-line purchase card; A1's `CardInputResolver` reads the line's
  `candidate_item_id` as the purchase target. 「不是」 never reaches a builder: the
  approval service closes the card without writing.

B1 puts this router in `get_approval_service` (`app/core/dependencies.py`).
"""

from __future__ import annotations

from app.models.agent import ApprovalPresentation
from app.services.approvals import (
    SAME_ITEM,
    ApprovalRecord,
    DomainWriteBuilder,
    DomainWritePlan,
)
from app.services.purchase_inputs import UnsupportedAction, is_photo_card

PHOTO_WRITE_MISSING = "Photo new-item cards cannot be written yet (Issue #124)."
NOT_SAME_ITEM_WRITES_NOTHING = "An item-match card answered 「不是」 writes nothing (section 10)."


class CardWriteRouter:
    """A `DomainWriteBuilder` that hands each card to the builder that writes it."""

    def __init__(
        self,
        purchases: DomainWriteBuilder,
        photo_items: DomainWriteBuilder | None = None,
    ) -> None:
        self._purchases = purchases
        self._photo_items = photo_items

    async def build(self, record: ApprovalRecord) -> DomainWritePlan:
        if record.presentation is ApprovalPresentation.ITEM_MATCH:
            if record.selected_option_id != SAME_ITEM:
                raise UnsupportedAction(NOT_SAME_ITEM_WRITES_NOTHING)
            return await self._purchases.build(_as_purchase_card(record))
        if is_photo_card(record):
            if self._photo_items is None:
                raise UnsupportedAction(PHOTO_WRITE_MISSING)
            return await self._photo_items.build(record)
        return await self._purchases.build(record)


def _as_purchase_card(record: ApprovalRecord) -> ApprovalRecord:
    """An item-match card answered 「是」, as the one-line purchase card it confirms.

    Same card, line, IDs, versions and confirmation time; only the presentation and the
    answered option go, because C1's builder plans resolved purchase cards only.
    """
    return record.model_copy(
        update={
            "presentation": ApprovalPresentation.SINGLE_ACTION,
            "options": [],
            "selected_option_id": None,
        }
    )
