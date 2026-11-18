"""C1 domain plans only: no authorization, persistence or public write API.

Stable identities, timestamps and versioned snapshots must come from the
server-stored proposal. Rebuilding with fresh snapshots is not a write retry.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.models.domain import (
    ChangeEvent,
    InventoryItem,
    ShoppingItem,
    ShoppingStatus,
    Transaction,
    TransactionReason,
)
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.shopping.preview import (
    ShoppingDecisionRequired,
    ShoppingPreviewError,
    ShoppingPreviewService,
    _pending_item,
    _quantity,
)


@dataclass(frozen=True)
class NewInventoryTarget:
    """Exact confirmed values; item_id is supplied by B1's stable backend resolver.

    This is a create intent, never a fabricated existing snapshot or ETag.
    """

    item_id: str
    name: str
    unit: str
    location: str | None


@dataclass(frozen=True)
class PurchaseInput:
    operation_id: str
    transaction_id: str
    shopping: ShoppingItem
    shopping_etag: str
    inventory: InventoryItem | None
    inventory_etag: str | None
    purchased_quantity: int
    purchased_unit: str | None = None
    resolved_inventory_item_id: str | None = None
    purchased_at: datetime | None = None
    new_inventory: NewInventoryTarget | None = None


@dataclass(frozen=True)
class AdjustmentInput:
    """A confirmed standalone stock addition with no Shopping row.

    This input must be resolved from A1's stored approval, never from mutable
    client values. Every identity and value comes from the stored approval card;
    B1's atomic repository and A1's resolver remain separate dependencies.
    """

    operation_id: str
    transaction_id: str
    approval_id: str
    new_inventory: NewInventoryTarget
    quantity: int


@dataclass(frozen=True)
class ExistingInventoryAdjustmentInput:
    """Approved positive quantity added to an existing Inventory snapshot.

    The caller must resolve every field from the stored approval. This release
    fixes the reason to ``adjustment``; it must never be inferred from the source
    channel or changed by the client.
    """

    operation_id: str
    transaction_id: str
    approval_id: str
    inventory: InventoryItem
    expected_etag: str
    inventory_quantity_before: int
    quantity_added: int
    reason: TransactionReason


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
class InventoryCreate:
    operation_id: str
    record: InventoryItem


@dataclass(frozen=True)
class PurchasePlan:
    household_id: str
    shopping_updates: tuple[ShoppingUpdate, ...]
    inventory_updates: tuple[InventoryUpdate, ...]
    transactions: tuple[Transaction, ...]
    inventory_creates: tuple[InventoryCreate, ...] = ()


@dataclass(frozen=True)
class AdjustmentPlan:
    """Pure domain plan for any approved standalone new inventory item.

    The returned domain records are grouped per operation by the batch builder;
    ApprovalService and B1's repository still own authorization and persistence.
    """

    household_id: str
    operation_id: str
    approval_id: str
    inventory_create: InventoryCreate
    transaction: Transaction
    change_event: ChangeEvent
    recorded_at: datetime


@dataclass(frozen=True)
class ExistingInventoryAdjustmentPlan:
    """Pure plan to increase an existing confirmed quantity with ETag guard."""

    household_id: str
    operation_id: str
    approval_id: str
    inventory_update: InventoryUpdate
    transaction: Transaction
    change_event: ChangeEvent
    recorded_at: datetime


@dataclass(frozen=True)
class ExistingInventoryAdjustmentBatchPlan:
    """Grouped plans for approved lines, combining updates by Inventory item."""

    household_id: str
    inventory_updates: tuple[InventoryUpdate, ...]
    transactions: tuple[Transaction, ...]
    change_events: tuple[ChangeEvent, ...]
    recorded_at: datetime


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
        """Use recorded_at as the stored confirmation time, never the retry clock.

        A dated purchase uses its per-operation purchased_at for the Transaction;
        otherwise it uses recorded_at. Domain updated_at always uses recorded_at.
        These are provisional #60 semantics pending A1/B1/B2 alignment.
        """
        _identity(household_id, "Household")
        _timestamp(recorded_at)
        if not purchases:
            raise ShoppingPreviewError("A purchase plan requires a non-empty selection.")
        operation_ids: set[str] = set()
        transaction_ids: set[str] = set()
        shopping_ids: set[str] = set()
        sources: dict[str, PurchaseInput] = {}
        deltas: dict[str, list[tuple[str, int]]] = {}
        shopping_updates: list[ShoppingUpdate] = []
        transactions: list[Transaction] = []
        preview_service = ShoppingPreviewService()
        inventory_creates: list[InventoryCreate] = []
        new_ids: set[str] = set()

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
            purchased_at = (
                purchase.purchased_at if purchase.purchased_at is not None else recorded_at
            )
            _timestamp(purchased_at)
            if purchased_at > recorded_at:
                raise ShoppingPreviewError("Purchase time must not be after confirmation time.")
            if purchase.new_inventory is not None:
                target = purchase.new_inventory
                if purchase.inventory is not None or purchase.inventory_etag is not None:
                    raise ShoppingPreviewError(
                        "A new target cannot have an existing snapshot/ETag."
                    )
                if purchase.shopping.item_id is not None:
                    raise ShoppingPreviewError("A linked shopping item cannot create a new target.")
                for value, label in (
                    (target.item_id, "New Inventory ID"),
                    (target.name, "New Inventory name"),
                    (target.unit, "New Inventory unit"),
                ):
                    _identity(value, label)
                if target.item_id != resolve_inventory_item_id(purchase.operation_id):
                    raise ShoppingPreviewError(
                        "New Inventory ID must come from B1's stable operation resolver."
                    )
                if target.location is not None:
                    _identity(target.location, "New Inventory location")
                if purchase.resolved_inventory_item_id not in (None, target.item_id):
                    raise ShoppingPreviewError("New target and resolved target must agree.")
                # Validate the entry; its planned quantity is not a purchase ceiling.
                _pending_item(household_id, purchase.shopping)
                quantity = _quantity(purchase.purchased_quantity, "Purchased quantity")
                if purchase.purchased_unit not in (None, target.unit) or (
                    purchase.shopping.unit is not None and purchase.shopping.unit != target.unit
                ):
                    raise ShoppingDecisionRequired("Resolve canonical units before a new purchase.")
                item_id = target.item_id
                if item_id in new_ids or item_id in sources:
                    raise ShoppingPreviewError("New Inventory targets must be distinct and absent.")
                new_ids.add(item_id)
                inventory_creates.append(
                    InventoryCreate(
                        operation_id=purchase.operation_id,
                        record=InventoryItem(
                            item_id=item_id,
                            household_id=household_id,
                            name=target.name,
                            quantity=quantity,
                            unit=target.unit,
                            location=target.location,
                            created_at=recorded_at,
                            updated_at=recorded_at,
                        ),
                    )
                )
                next_status = ShoppingStatus.COMPLETED
            else:
                if purchase.inventory is None:
                    raise ShoppingDecisionRequired("Resolve an existing or explicit new target.")
                _identity(purchase.inventory_etag, "Inventory ETag")
                preview = preview_service.preview_purchase(
                    household_id=household_id,
                    shopping=purchase.shopping,
                    inventory=purchase.inventory,
                    purchased_quantity=purchase.purchased_quantity,
                    purchased_unit=purchase.purchased_unit,
                    resolved_inventory_item_id=purchase.resolved_inventory_item_id,
                )
                quantity, next_status = preview.purchased_quantity, preview.next_status
                item_id = purchase.inventory.item_id
                if item_id in new_ids:
                    raise ShoppingPreviewError("A target cannot be both created and updated.")
                previous = sources.get(item_id)
                if previous is not None and (
                    previous.inventory != purchase.inventory
                    or previous.inventory_etag != purchase.inventory_etag
                ):
                    raise ShoppingPreviewError("Shared inventory snapshots and ETags must agree.")
                sources[item_id] = purchase
                deltas.setdefault(item_id, []).append((purchase.operation_id, quantity))
            shopping_updates.append(
                ShoppingUpdate(
                    operation_id=purchase.operation_id,
                    record=purchase.shopping.model_copy(
                        update={"status": next_status, "updated_at": recorded_at}
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
                    quantity_delta=quantity,
                    reason=TransactionReason.PURCHASE,
                    created_at=purchased_at,
                )
            )

        inventory_updates: list[InventoryUpdate] = []
        for item_id, source in sources.items():
            changes = deltas[item_id]
            # The preview validated and normalized every count; Python integer
            # addition preserves exact stock even beyond float's precision.
            quantity = int(source.inventory.quantity) + sum(delta for _, delta in changes)
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
            inventory_creates=tuple(inventory_creates),
        )

    def plan_adjustment(
        self,
        *,
        household_id: str,
        adjustment: AdjustmentInput,
        recorded_at: datetime,
    ) -> AdjustmentPlan:
        """Plan a standalone incoming-stock addition from an approved proposal.

        This only creates immutable domain values, including the provenance event.
        It performs no authorization, Cosmos writes, or public API integration.
        """
        _identity(household_id, "Household")
        _identity(adjustment.operation_id, "Operation ID")
        _identity(adjustment.transaction_id, "Transaction ID")
        _identity(adjustment.approval_id, "Approval ID")
        _timestamp(recorded_at)

        target = adjustment.new_inventory
        for value, label in (
            (target.item_id, "New Inventory ID"),
            (target.name, "New Inventory name"),
            (target.unit, "New Inventory unit"),
        ):
            _identity(value, label)
        if target.item_id != resolve_inventory_item_id(adjustment.operation_id):
            raise ShoppingPreviewError(
                "New Inventory ID must come from B1's stable operation resolver."
            )
        if target.location is not None:
            _identity(target.location, "New Inventory location")
        quantity = _quantity(adjustment.quantity, "Adjustment quantity")

        inventory_create = InventoryCreate(
            operation_id=adjustment.operation_id,
            record=InventoryItem(
                item_id=target.item_id,
                household_id=household_id,
                name=target.name,
                quantity=quantity,
                unit=target.unit,
                location=target.location,
                created_at=recorded_at,
                updated_at=recorded_at,
            ),
        )
        transaction = Transaction(
            transaction_id=adjustment.transaction_id,
            household_id=household_id,
            item_id=target.item_id,
            operation_id=adjustment.operation_id,
            quantity_delta=quantity,
            reason=TransactionReason.ADJUSTMENT,
            created_at=recorded_at,
        )
        change_event = ChangeEvent(
            event_id=adjustment.operation_id,
            household_id=household_id,
            operation_id=adjustment.operation_id,
            approval_id=adjustment.approval_id,
            transaction_id=adjustment.transaction_id,
            shopping_item_id=None,
            item_id=target.item_id,
            occurred_at=recorded_at,
            recorded_at=recorded_at,
        )
        return AdjustmentPlan(
            household_id=household_id,
            operation_id=adjustment.operation_id,
            approval_id=adjustment.approval_id,
            inventory_create=inventory_create,
            transaction=transaction,
            change_event=change_event,
            recorded_at=recorded_at,
        )

    def plan_existing_inventory_adjustment(
        self,
        *,
        household_id: str,
        adjustment: ExistingInventoryAdjustmentInput,
        recorded_at: datetime,
    ) -> ExistingInventoryAdjustmentPlan:
        """Plan a confirmed positive increment to an existing item.

        The transaction reason must be stored in the approval and may only be
        ``purchase`` or ``adjustment``. This function does not authorize,
        persist, refresh stale snapshots, or retry with a new ETag.
        """
        batch = self.plan_existing_inventory_adjustments(
            household_id=household_id,
            adjustments=(adjustment,),
            recorded_at=recorded_at,
        )
        return ExistingInventoryAdjustmentPlan(
            household_id=household_id,
            operation_id=adjustment.operation_id,
            approval_id=adjustment.approval_id,
            inventory_update=batch.inventory_updates[0],
            transaction=batch.transactions[0],
            change_event=batch.change_events[0],
            recorded_at=recorded_at,
        )

    def plan_existing_inventory_adjustments(
        self,
        *,
        household_id: str,
        adjustments: Sequence[ExistingInventoryAdjustmentInput],
        recorded_at: datetime,
    ) -> ExistingInventoryAdjustmentBatchPlan:
        """Plan approved stock additions and combine Inventory writes per item."""
        _identity(household_id, "Household")
        _timestamp(recorded_at)
        if not adjustments:
            raise ShoppingPreviewError("At least one existing Inventory adjustment is required.")

        seen_operations: set[str] = set()
        seen_transactions: set[str] = set()
        quantities: dict[str, int] = {}
        base_quantities: dict[str, int] = {}
        snapshots: dict[str, InventoryItem] = {}
        etags: dict[str, str] = {}
        operation_ids: dict[str, list[str]] = {}
        transactions: list[Transaction] = []
        change_events: list[ChangeEvent] = []

        for adjustment in adjustments:
            _identity(adjustment.operation_id, "Operation ID")
            _identity(adjustment.transaction_id, "Transaction ID")
            _identity(adjustment.approval_id, "Approval ID")
            _identity(adjustment.expected_etag, "Inventory ETag")
            if adjustment.operation_id in seen_operations:
                raise ShoppingPreviewError("Adjustment operation IDs must be unique.")
            if adjustment.transaction_id in seen_transactions:
                raise ShoppingPreviewError("Adjustment transaction IDs must be unique.")
            seen_operations.add(adjustment.operation_id)
            seen_transactions.add(adjustment.transaction_id)

            inventory = adjustment.inventory
            if inventory.household_id != household_id:
                raise ShoppingPreviewError(
                    "Inventory target must belong to the requested household."
                )
            if inventory.quantity != adjustment.inventory_quantity_before:
                raise ShoppingPreviewError(
                    "Card-bound inventory_quantity_before must match the Inventory snapshot."
                )
            if adjustment.reason != TransactionReason.ADJUSTMENT:
                raise ShoppingPreviewError("Existing stock additions require adjustment reason.")
            quantity_before = _quantity(
                adjustment.inventory_quantity_before,
                "Inventory quantity before",
                positive=False,
            )
            _quantity(inventory.quantity, "Inventory snapshot quantity", positive=False)
            quantity = _quantity(adjustment.quantity_added, "Adjustment quantity")

            item_id = inventory.item_id
            previous_snapshot = snapshots.get(item_id)
            if previous_snapshot is not None and (
                previous_snapshot != inventory or etags[item_id] != adjustment.expected_etag
            ):
                raise ShoppingPreviewError(
                    "Lines for one Inventory item must use the same snapshot and ETag."
                )
            snapshots[item_id] = inventory
            etags[item_id] = adjustment.expected_etag
            base_quantities[item_id] = quantity_before
            quantities[item_id] = quantities.get(item_id, 0) + quantity
            operation_ids.setdefault(item_id, []).append(adjustment.operation_id)

            transactions.append(
                Transaction(
                    transaction_id=adjustment.transaction_id,
                    household_id=household_id,
                    item_id=item_id,
                    operation_id=adjustment.operation_id,
                    quantity_delta=quantity,
                    reason=adjustment.reason,
                    created_at=recorded_at,
                )
            )
            change_events.append(
                ChangeEvent(
                    event_id=adjustment.operation_id,
                    household_id=household_id,
                    operation_id=adjustment.operation_id,
                    approval_id=adjustment.approval_id,
                    transaction_id=adjustment.transaction_id,
                    shopping_item_id=None,
                    item_id=item_id,
                    occurred_at=recorded_at,
                    recorded_at=recorded_at,
                )
            )

        inventory_updates = tuple(
            InventoryUpdate(
                record=snapshots[item_id].model_copy(
                    update={
                        "quantity": base_quantities[item_id] + quantities[item_id],
                        "updated_at": recorded_at,
                    }
                ),
                expected_etag=etags[item_id],
                operation_ids=tuple(operation_ids[item_id]),
            )
            for item_id in snapshots
        )
        return ExistingInventoryAdjustmentBatchPlan(
            household_id=household_id,
            inventory_updates=inventory_updates,
            transactions=tuple(transactions),
            change_events=tuple(change_events),
            recorded_at=recorded_at,
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
