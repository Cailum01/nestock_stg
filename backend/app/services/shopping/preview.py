"""Pure, provisional C1 business previews, not approved executable commands.

These internal results deliberately do not redefine ShoppingItem.quantity or
construct persisted Inventory/Transaction records. See README.md in this folder.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus


class ShoppingPreviewError(ValueError):
    """The supplied snapshots cannot produce an unambiguous preview."""


class ShoppingDecisionRequired(ShoppingPreviewError):
    """An unresolved workflow needs clarification or cross-owner agreement."""


@dataclass(frozen=True)
class PurchasePreview:
    household_id: str
    shopping_item_id: str
    inventory_item_id: str
    purchased_quantity: float
    unit: str
    planned_quantity: float
    next_status: ShoppingStatus
    inventory_quantity_before: float
    inventory_quantity_after: float


@dataclass(frozen=True)
class RemovalPreview:
    household_id: str
    shopping_item_id: str
    name: str
    planned_quantity: float
    unit: str


def _quantity(value: float, label: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShoppingPreviewError(f"{label} must be a finite number.")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise ShoppingPreviewError(f"{label} is outside the supported quantity range.") from error
    if not finite or (value <= 0 if positive else value < 0):
        raise ShoppingPreviewError(f"{label} is outside the supported quantity range.")
    # Avoid binary addition artifacts in the proposed stock (e.g. 0.1 + 0.2).
    # This is local preview arithmetic, not a shared persistence precision policy.
    return Decimal(str(value))


def _pending_item(household_id: str, shopping: ShoppingItem) -> Decimal:
    if not household_id.strip() or shopping.household_id != household_id:
        raise ShoppingPreviewError("Shopping item must belong to the requested household.")
    if not shopping.shopping_item_id.strip() or not shopping.unit.strip():
        raise ShoppingPreviewError("Shopping identity and unit must be resolved.")
    if shopping.status != ShoppingStatus.PENDING:
        raise ShoppingPreviewError("Only a pending shopping item can be previewed.")
    return _quantity(shopping.quantity, "Planned shopping quantity")


class ShoppingPreviewService:
    """Resolve display-only changes without I/O, mutation, IDs or approval claims."""

    def preview_purchase(
        self,
        *,
        household_id: str,
        shopping: ShoppingItem,
        inventory: InventoryItem | None,
        purchased_quantity: float,
    ) -> PurchasePreview:
        """Preview a current purchase already resolved to an existing inventory item.

        Quantity must be user-reported in the resolved system unit. This method
        cannot establish provenance, authorization, freshness or persistence.
        Unlinked purchases are deferred, not prohibited as a product feature.
        """
        planned = _pending_item(household_id, shopping)
        purchased = _quantity(purchased_quantity, "Purchased quantity")
        if purchased > planned:
            raise ShoppingDecisionRequired("Purchases exceeding the list need an agreed workflow.")
        if inventory is None or not shopping.item_id:
            raise ShoppingDecisionRequired("Unlinked purchase workflow is not resolved here.")
        if inventory.household_id != household_id:
            raise ShoppingPreviewError("Inventory must belong to the requested household.")
        if not inventory.item_id.strip() or inventory.item_id != shopping.item_id:
            raise ShoppingPreviewError("Inventory must match the shopping item's resolved target.")
        if inventory.unit != shopping.unit:
            raise ShoppingDecisionRequired("Resolve canonical units before previewing a purchase.")
        before = _quantity(inventory.quantity, "Confirmed inventory quantity", positive=False)
        after = float(before + purchased)
        if not math.isfinite(after):
            raise ShoppingPreviewError("Resulting inventory quantity must be finite.")
        return PurchasePreview(
            household_id=household_id,
            shopping_item_id=shopping.shopping_item_id,
            inventory_item_id=inventory.item_id,
            purchased_quantity=float(purchased),
            unit=inventory.unit,
            planned_quantity=float(planned),
            next_status=ShoppingStatus.COMPLETED,
            inventory_quantity_before=float(before),
            inventory_quantity_after=after,
        )

    def preview_removal(self, *, household_id: str, shopping: ShoppingItem) -> RemovalPreview:
        """Describe removal of a pending entry; never implies purchase or stock reversal."""
        planned = _pending_item(household_id, shopping)
        return RemovalPreview(
            household_id=household_id,
            shopping_item_id=shopping.shopping_item_id,
            name=shopping.name,
            planned_quantity=float(planned),
            unit=shopping.unit,
        )
