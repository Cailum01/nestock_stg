"""C1 domain plans only: no authorization, persistence or public write API.

Stable identities, timestamps and versioned snapshots must come from the
server-stored proposal. Rebuilding with fresh snapshots is not a write retry.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.models.domain import InventoryItem, ShoppingItem, Transaction, TransactionReason
from app.services.shopping.preview import ShoppingPreviewError, ShoppingPreviewService


@dataclass(frozen=True)
class PurchaseInput:
    operation_id: str
    transaction_id: str
    shopping: ShoppingItem
    shopping_etag: str
    inventory: InventoryItem
    inventory_etag: str
    purchased_quantity: float


@dataclass(frozen=True)
class ShoppingUpdate:
    operation_id: str
    record: ShoppingItem
    expected_etag: str


@dataclass(frozen=True)
class InventoryUpdate:
    record: InventoryItem
    expected_etag: str
    operation_ids: tuple[str, ...]


@dataclass(frozen=True)
class PurchasePlan:
    household_id: str
    shopping_updates: tuple[ShoppingUpdate, ...]
    inventory_updates: tuple[InventoryUpdate, ...]
    transactions: tuple[Transaction, ...]


@dataclass(frozen=True)
class RemovalPlan:
    household_id: str
    operation_id: str
    shopping_item_id: str
    expected_etag: str
    deleted_at: datetime


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ShoppingPreviewError(f"{label} must be resolved and non-empty.")


def _timestamp(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ShoppingPreviewError("A stable timezone-aware proposal timestamp is required.")


class ShoppingPlanService:
    """Prepare the entire selected group without I/O or any approval claim.

    The caller selects inputs from the stored proposal, never from client edits.
    A1 must validate that binding; B1 must enforce ETags and atomic persistence.
    """

    def plan_purchase(
        self,
        *,
        household_id: str,
        purchases: Sequence[PurchaseInput],
        recorded_at: datetime,
    ) -> PurchasePlan:
        _identity(household_id, "Household")
        _timestamp(recorded_at)
        if not purchases:
            raise ShoppingPreviewError("A purchase plan requires a non-empty selection.")
        operation_ids: set[str] = set()
        transaction_ids: set[str] = set()
        shopping_ids: set[str] = set()
        sources: dict[str, PurchaseInput] = {}
        deltas: dict[str, list[tuple[str, float]]] = {}
        shopping_updates: list[ShoppingUpdate] = []
        transactions: list[Transaction] = []
        preview_service = ShoppingPreviewService()

        for purchase in purchases:
            for value, label, seen in (
                (purchase.operation_id, "Operation ID", operation_ids),
                (purchase.transaction_id, "Transaction ID", transaction_ids),
                (purchase.shopping.shopping_item_id, "Shopping ID", shopping_ids),
            ):
                _identity(value, label)
                if value in seen:
                    raise ShoppingPreviewError(f"Duplicate {label} in purchase selection.")
                seen.add(value)
            _identity(purchase.shopping_etag, "Shopping ETag")
            _identity(purchase.inventory_etag, "Inventory ETag")
            preview = preview_service.preview_purchase(
                household_id=household_id,
                shopping=purchase.shopping,
                inventory=purchase.inventory,
                purchased_quantity=purchase.purchased_quantity,
            )
            item_id = purchase.inventory.item_id
            previous = sources.get(item_id)
            if previous is not None and (
                previous.inventory != purchase.inventory
                or previous.inventory_etag != purchase.inventory_etag
            ):
                raise ShoppingPreviewError("Shared inventory snapshots and ETags must agree.")
            sources[item_id] = purchase
            deltas.setdefault(item_id, []).append(
                (purchase.operation_id, preview.purchased_quantity)
            )
            shopping_updates.append(
                ShoppingUpdate(
                    operation_id=purchase.operation_id,
                    record=purchase.shopping.model_copy(
                        update={"status": preview.next_status, "updated_at": recorded_at}
                    ),
                    expected_etag=purchase.shopping_etag,
                )
            )
            transactions.append(
                Transaction(
                    transaction_id=purchase.transaction_id,
                    household_id=household_id,
                    item_id=item_id,
                    operation_id=purchase.operation_id,
                    quantity_delta=preview.purchased_quantity,
                    reason=TransactionReason.PURCHASE,
                    created_at=recorded_at,
                )
            )

        inventory_updates: list[InventoryUpdate] = []
        for item_id, source in sources.items():
            changes = deltas[item_id]
            quantity = float(
                Decimal(str(source.inventory.quantity))
                + sum((Decimal(str(delta)) for _, delta in changes), Decimal(0))
            )
            if not math.isfinite(quantity):
                raise ShoppingPreviewError("Combined inventory quantity must be finite.")
            inventory_updates.append(
                InventoryUpdate(
                    record=source.inventory.model_copy(
                        update={"quantity": quantity, "updated_at": recorded_at}
                    ),
                    expected_etag=source.inventory_etag,
                    operation_ids=tuple(operation_id for operation_id, _ in changes),
                )
            )
        return PurchasePlan(
            household_id=household_id,
            shopping_updates=tuple(shopping_updates),
            inventory_updates=tuple(inventory_updates),
            transactions=tuple(transactions),
        )

    def plan_removal(
        self,
        *,
        household_id: str,
        operation_id: str,
        shopping: ShoppingItem,
        shopping_etag: str,
        recorded_at: datetime,
    ) -> RemovalPlan:
        _identity(operation_id, "Operation ID")
        _identity(shopping_etag, "Shopping ETag")
        _timestamp(recorded_at)
        ShoppingPreviewService().preview_removal(household_id=household_id, shopping=shopping)
        return RemovalPlan(
            household_id=household_id,
            operation_id=operation_id,
            shopping_item_id=shopping.shopping_item_id,
            expected_etag=shopping_etag,
            deleted_at=recorded_at,
        )
