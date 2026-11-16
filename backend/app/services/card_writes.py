"""A1: which builder plans the writes for a confirmed card (`/agent/resume`, Issue #124).

`ApprovalService` takes one `DomainWriteBuilder`. A1's cards are of two kinds:

- Purchase cards (v8 flow C, contract section 11) go to C1's `ShoppingWriteBuilder`,
  as before.
- Photo new-item cards (contract section 10) have no Shopping List entry. They go to
  B1's builder for a new item without one (Issue #124): it takes the lines from A1's
  `PhotoInputResolver`, plans each with C1's `plan_adjustment` and writes the item,
  the `adjustment` Transaction, the change event and the receipts with the card in
  one batch: `PhotoItemWriteBuilder` (PR #171). Without it, a photo card is refused
  here and nothing is written.

B1 puts this router in `get_approval_service` (`app/core/dependencies.py`).
"""

from __future__ import annotations

from app.services.approvals import ApprovalRecord, DomainWriteBuilder, DomainWritePlan
from app.services.purchase_inputs import UnsupportedAction, is_photo_card

PHOTO_WRITE_MISSING = "Photo new-item cards cannot be written yet (Issue #124)."


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
        if is_photo_card(record):
            if self._photo_items is None:
                raise UnsupportedAction(PHOTO_WRITE_MISSING)
            return await self._photo_items.build(record)
        return await self._purchases.build(record)
