"""B1 persistence plan for approved new Inventory items without Shopping rows."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pydantic import JsonValue

from app.models.agent import ApprovalPresentation
from app.models.domain import TransactionReason
from app.services.approvals import (
    ApprovalRecord,
    ApprovalStatus,
    DomainWritePlan,
)
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_write import CreateDocument, change_event_create
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.shopping.planning import (
    AdjustmentInput,
    AdjustmentPlan,
    ShoppingPlanService,
)
from app.services.shopping.preview import ShoppingPreviewError

CREATE_ITEM = "create_item"


class PhotoAdjustmentInputResolver(Protocol):
    """Resolve approved values from A1's stored photo card, never client input."""

    def resolve(self, record: ApprovalRecord) -> Sequence[AdjustmentInput]: ...


class AdjustmentPlanner(Protocol):
    """C1's pure planner for no-Shopping inventory adjustments."""

    def plan_adjustment(
        self,
        *,
        household_id: str,
        adjustment: AdjustmentInput,
        recorded_at: datetime,
    ) -> AdjustmentPlan: ...


class PhotoItemWriteBuilder:
    """Build the atomic domain writes for a confirmed no-Shopping create-item card.

    ApprovalService adds operation receipts and the completed approval to this plan
    and commits the whole group through B1's transactional repository.
    """

    def __init__(
        self,
        resolver: PhotoAdjustmentInputResolver,
        planner: AdjustmentPlanner | None = None,
    ) -> None:
        self._resolver = resolver
        self._planner = planner or ShoppingPlanService()

    async def build(self, record: ApprovalRecord) -> DomainWritePlan:
        if record.status not in (ApprovalStatus.PENDING, ApprovalStatus.CONFIRMED):
            raise ShoppingPreviewError("Only a pending trial or confirmed card can be planned.")
        if record.presentation not in (
            ApprovalPresentation.SINGLE_ACTION,
            ApprovalPresentation.BATCH_ACTION,
        ):
            raise ShoppingPreviewError("Photo item writes require a single or batch action card.")
        if record.decided_at is None:
            raise ShoppingPreviewError("A fixed confirmation time is required.")
        if record.options:
            raise ShoppingPreviewError("Photo item cards cannot contain options.")

        operation_ids = [operation.operation_id for operation in record.operations]
        selected_ids = record.approved_operation_ids or []
        if (
            len(set(operation_ids)) != len(operation_ids)
            or not selected_ids
            or len(set(selected_ids)) != len(selected_ids)
            or not set(selected_ids) <= set(operation_ids)
        ):
            raise ShoppingPreviewError("The fixed photo item selection is invalid.")

        selected_operations = [
            operation
            for operation in record.operations
            if operation.operation_id in set(selected_ids)
        ]
        if [operation.operation_id for operation in selected_operations] != selected_ids:
            raise ShoppingPreviewError("Selected operations must retain card order.")
        if any(
            operation.action != CREATE_ITEM
            or operation.targets
            or operation.values.get("shopping_item_id")
            for operation in record.operations
        ):
            raise ShoppingPreviewError(
                "This builder accepts only new-item cards without Shopping targets."
            )

        adjustments = tuple(self._resolver.resolve(record))
        if [adjustment.operation_id for adjustment in adjustments] != selected_ids:
            raise ShoppingPreviewError(
                "Resolved adjustments must exactly match the approved card selection."
            )

        stored_by_id = {operation.operation_id: operation for operation in record.operations}
        operations_by_id: dict[str, tuple[CreateDocument, CreateDocument, CreateDocument]] = {}
        outcomes: dict[str, dict[str, JsonValue]] = {}
        transaction_ids: set[str] = set()
        item_ids: set[str] = set()

        for adjustment in adjustments:
            operation = stored_by_id[adjustment.operation_id]
            values = operation.values
            target = adjustment.new_inventory
            if (
                adjustment.approval_id != record.approval_id
                or adjustment.transaction_id != values.get("transaction_id")
                or adjustment.quantity != values.get("quantity")
                or target.name != values.get("name")
                or target.unit != values.get("unit")
                or target.location != values.get("location")
                or target.item_id != resolve_inventory_item_id(adjustment.operation_id)
            ):
                raise ShoppingPreviewError(
                    "Resolved adjustment values must match the stored approved card."
                )
            if adjustment.transaction_id in transaction_ids or target.item_id in item_ids:
                raise ShoppingPreviewError("Photo item identities must be unique per card.")
            transaction_ids.add(adjustment.transaction_id)
            item_ids.add(target.item_id)

            plan = self._planner.plan_adjustment(
                household_id=record.household_id,
                adjustment=adjustment,
                recorded_at=record.decided_at,
            )
            self._validate_plan(record, adjustment, plan)

            inventory = plan.inventory_create.record
            transaction = plan.transaction
            event = plan.change_event
            operations_by_id[plan.operation_id] = (
                CreateDocument(CosmosDocumentMapper.to_document(inventory)),
                CreateDocument(CosmosDocumentMapper.to_document(transaction)),
                change_event_create(event),
            )
            outcomes[plan.operation_id] = {
                "item_id": inventory.item_id,
                "transaction_id": transaction.transaction_id,
                "quantity": inventory.quantity,
                "unit": inventory.unit,
            }

        return DomainWritePlan(operations_by_id=operations_by_id, outcome=outcomes)

    @staticmethod
    def _validate_plan(
        record: ApprovalRecord,
        adjustment: AdjustmentInput,
        plan: AdjustmentPlan,
    ) -> None:
        inventory = plan.inventory_create.record
        transaction = plan.transaction
        event = plan.change_event
        if (
            plan.household_id != record.household_id
            or plan.operation_id != adjustment.operation_id
            or plan.approval_id != record.approval_id
            or plan.recorded_at != record.decided_at
            or plan.inventory_create.operation_id != adjustment.operation_id
            or inventory.household_id != record.household_id
            or inventory.item_id != adjustment.new_inventory.item_id
            or inventory.name != adjustment.new_inventory.name
            or inventory.quantity != adjustment.quantity
            or inventory.unit != adjustment.new_inventory.unit
            or inventory.location != adjustment.new_inventory.location
            or inventory.created_at != record.decided_at
            or inventory.updated_at != record.decided_at
            or transaction.household_id != record.household_id
            or transaction.operation_id != adjustment.operation_id
            or transaction.transaction_id != adjustment.transaction_id
            or transaction.item_id != inventory.item_id
            or transaction.reason is not TransactionReason.ADJUSTMENT
            or transaction.quantity_delta != inventory.quantity
            or transaction.created_at != record.decided_at
            or event.event_id != adjustment.operation_id
            or event.household_id != record.household_id
            or event.operation_id != adjustment.operation_id
            or event.approval_id != record.approval_id
            or event.transaction_id != adjustment.transaction_id
            or event.shopping_item_id is not None
            or event.item_id != adjustment.new_inventory.item_id
            or event.occurred_at != transaction.created_at
            or event.recorded_at != record.decided_at
        ):
            raise ShoppingPreviewError("The adjustment plan does not match the approved card.")
