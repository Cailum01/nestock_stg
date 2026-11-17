"""Pure, provisional C1 business previews, not approved executable commands.

These internal results deliberately do not redefine ShoppingItem.quantity or
construct persisted Inventory/Transaction records. See README.md in this folder.
"""

import math
from dataclasses import dataclass

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
    purchased_quantity: int
    unit: str
    next_status: ShoppingStatus
    inventory_quantity_before: int
    inventory_quantity_after: int


@dataclass(frozen=True)
class RemovalPreview:
    household_id: str
    shopping_item_id: str
    name: str
    unit: str | None


def _quantity(value: int, label: str, *, positive: bool = True) -> int:
    # Accept integral floats from the pre-#64 Pydantic snapshots, never truncate.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShoppingPreviewError(f"{label} must be an integer count.")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ShoppingPreviewError(f"{label} must be an integer count.")
    if (value <= 0) if positive else (value < 0):
        raise ShoppingPreviewError(f"{label} is outside the supported quantity range.")
    return int(value)


def _pending_item(household_id: str, shopping: ShoppingItem) -> None:
    if not household_id.strip() or shopping.household_id != household_id:
        raise ShoppingPreviewError("Shopping item must belong to the requested household.")
    if not shopping.shopping_item_id.strip():
        raise ShoppingPreviewError("Shopping identity must be resolved.")
    if shopping.unit is not None and not shopping.unit.strip():
        raise ShoppingPreviewError("A provided shopping unit must not be empty.")
    if shopping.status != ShoppingStatus.PENDING:
        raise ShoppingPreviewError("Only a pending shopping item can be previewed.")


class ShoppingPreviewService:
    """Resolve display-only changes without I/O, mutation, IDs or approval claims."""

    def preview_purchase(
        self,
        *,
        household_id: str,
        shopping: ShoppingItem,
        inventory: InventoryItem | None,
        purchased_quantity: int,
        purchased_unit: str | None = None,
        resolved_inventory_item_id: str | None = None,
    ) -> PurchasePreview:
        """Preview a current purchase already resolved to an existing inventory item.

        Quantity must be user-reported in the resolved system unit. This method
        cannot establish provenance, authorization, freshness or persistence.
        Unlinked purchases are deferred, not prohibited as a product feature.
        """
        _pending_item(household_id, shopping)
        purchased = _quantity(purchased_quantity, "Purchased quantity")
        if resolved_inventory_item_id is not None and not resolved_inventory_item_id.strip():
            raise ShoppingPreviewError("The explicit resolved target must not be empty.")
        target_id = (
            resolved_inventory_item_id
            if resolved_inventory_item_id is not None
            else shopping.item_id
        )
        if inventory is None or not target_id:
            raise ShoppingDecisionRequired("Unlinked purchase workflow is not resolved here.")
        if inventory.household_id != household_id:
            raise ShoppingPreviewError("Inventory must belong to the requested household.")
        if (
            not inventory.item_id.strip()
            or inventory.item_id != target_id
            or (shopping.item_id is not None and shopping.item_id != target_id)
        ):
            raise ShoppingPreviewError("Inventory must match the shopping item's resolved target.")
        unit = purchased_unit if purchased_unit is not None else shopping.unit
        if not unit or not unit.strip():
            raise ShoppingDecisionRequired("Resolve the actual purchase unit before previewing.")
        if unit != inventory.unit or (shopping.unit is not None and unit != shopping.unit):
            raise ShoppingDecisionRequired("Resolve canonical units before previewing a purchase.")
        before = _quantity(inventory.quantity, "Confirmed inventory quantity", positive=False)
        after = before + purchased
        return PurchasePreview(
            household_id=household_id,
            shopping_item_id=shopping.shopping_item_id,
            inventory_item_id=inventory.item_id,
            purchased_quantity=purchased,
            unit=inventory.unit,
            next_status=ShoppingStatus.COMPLETED,
            inventory_quantity_before=before,
            inventory_quantity_after=after,
        )

    def preview_removal(self, *, household_id: str, shopping: ShoppingItem) -> RemovalPreview:
        """Describe removal of a pending entry; never implies purchase or stock reversal."""
        _pending_item(household_id, shopping)
        return RemovalPreview(
            household_id=household_id,
            shopping_item_id=shopping.shopping_item_id,
            name=shopping.name,
            unit=shopping.unit,
        )
