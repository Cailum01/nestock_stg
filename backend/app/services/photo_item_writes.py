"""B1 persistence plan for approved new Inventory items without Shopping rows."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from app.models.agent import ApprovalPresentation
from app.models.domain import (
    ChangeEvent,
    InventoryItem,
    MediaProcessingStatus,
    Transaction,
    TransactionReason,
)
from app.services.approvals import (
    ApprovalRecord,
    ApprovalStatus,
    DomainWritePlan,
    StoredOperation,
    TargetChanged,
)
from app.services.cosmos_read import CosmosDocumentMapper, MappedDocument
from app.services.cosmos_write import CreateDocument, ReplaceDocument, change_event_create
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.media_mapping import (
    MediaMappingPort,
    build_media_mapping_retention_update,
)
from app.services.media_retention_work import build_pending_media_retention_work_item
from app.services.shopping.planning import (
    AdjustmentInput,
    AdjustmentPlan,
    InventoryUpdate,
    ShoppingPlanService,
)
from app.services.shopping.preview import ShoppingPreviewError

CREATE_ITEM = "create_item"
INCREASE_STOCK = "increase_stock"


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


class ExistingInventoryReader(Protocol):
    async def read_inventory_item_with_etag(
        self, *, household_id: str, item_id: str
    ) -> MappedDocument[InventoryItem] | None: ...


class ExistingInventoryAdjustmentPlan(Protocol):
    household_id: str
    operation_id: str
    approval_id: str
    inventory_update: InventoryUpdate
    transaction: Transaction
    change_event: ChangeEvent
    recorded_at: datetime


@runtime_checkable
class ExistingInventoryAdjustmentPlanner(Protocol):
    def plan_existing_inventory_adjustment(
        self,
        *,
        household_id: str,
        operation_id: str,
        approval_id: str,
        transaction_id: str,
        inventory: InventoryItem,
        expected_etag: str,
        quantity_added: int,
        reason: TransactionReason,
        recorded_at: datetime,
    ) -> ExistingInventoryAdjustmentPlan: ...


class PhotoItemWriteBuilder:
    """Build atomic writes for approved no-Shopping Inventory stock cards.

    ApprovalService adds operation receipts and the completed approval to this plan
    and commits the whole group through B1's transactional repository.
    """

    def __init__(
        self,
        resolver: PhotoAdjustmentInputResolver,
        planner: AdjustmentPlanner | None = None,
        media_mapping_storage: MediaMappingPort | None = None,
    ) -> None:
        self._resolver = resolver
        self._planner = planner or ShoppingPlanService()
        self._media_mapping_storage = media_mapping_storage

    async def build(self, record: ApprovalRecord) -> DomainWritePlan:
        return await self._build(record, inventory_reader=None)

    async def build_stock_card(
        self,
        record: ApprovalRecord,
        inventory_reader: ExistingInventoryReader | None,
    ) -> DomainWritePlan:
        return await self._build(record, inventory_reader=inventory_reader)

    async def _build(
        self,
        record: ApprovalRecord,
        *,
        inventory_reader: ExistingInventoryReader | None,
    ) -> DomainWritePlan:
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
            operation.action not in (CREATE_ITEM, INCREASE_STOCK)
            or operation.values.get("shopping_item_id")
            for operation in record.operations
        ):
            raise ShoppingPreviewError(
                "Inventory stock cards accept only create_item and increase_stock lines."
            )

        stored_by_id = {operation.operation_id: operation for operation in record.operations}
        selected_create_operations = [
            operation for operation in selected_operations if operation.action == CREATE_ITEM
        ]
        selected_increase_operations = [
            operation for operation in selected_operations if operation.action == INCREASE_STOCK
        ]
        for operation in record.operations:
            if operation.action == CREATE_ITEM and operation.targets:
                raise ShoppingPreviewError("New-item lines cannot carry target versions.")
            if operation.action == INCREASE_STOCK:
                _stock_target(operation)

        operations_by_id: dict[str, tuple[CreateDocument, ...]] = {}
        outcomes: dict[str, dict[str, JsonValue]] = {}
        transaction_ids: set[str] = set()
        item_ids: set[str] = set()
        shared_replacements: dict[tuple[str, str], ReplaceDocument] = {}

        adjustments: tuple[AdjustmentInput, ...] = ()
        if selected_create_operations:
            create_ids = [operation.operation_id for operation in selected_create_operations]
            create_record = record.model_copy(
                update={
                    "operations": selected_create_operations,
                    "approved_operation_ids": create_ids,
                }
            )
            adjustments = tuple(self._resolver.resolve(create_record))
            if [adjustment.operation_id for adjustment in adjustments] != create_ids:
                raise ShoppingPreviewError(
                    "Resolved adjustments must exactly match the approved card selection."
                )

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
            media_values = _photo_media_values(operation.values)
            if media_values is not None:
                input_id, image_ref, mapping_etag = media_values
                if self._media_mapping_storage is None:
                    raise ShoppingPreviewError(
                        "Configure B1 media mapping storage before attaching a photo."
                    )
                mapping = await self._media_mapping_storage.find_completed_by_image_ref(
                    household_id=record.household_id,
                    image_ref=image_ref,
                )
                if (
                    mapping is None
                    or mapping.record.household_id != record.household_id
                    or mapping.record.input_id != input_id
                    or mapping.record.image_ref != image_ref
                    or mapping.record.status is not MediaProcessingStatus.COMPLETED
                    or mapping.etag != mapping_etag
                ):
                    raise TargetChanged(f"input:{input_id}")
                retention = build_media_mapping_retention_update(mapping, ttl_seconds=None)
                replacement_key = ("media", input_id)
                previous = shared_replacements.get(replacement_key)
                if previous is not None and previous != retention:
                    raise ShoppingPreviewError("Photo mapping updates must agree.")
                shared_replacements[replacement_key] = retention
                if inventory.image_ref not in (None, image_ref):
                    raise ShoppingPreviewError(
                        "The new inventory item already has a different photo."
                    )
                inventory = inventory.model_copy(update={"image_ref": image_ref})

            writes = [
                CreateDocument(CosmosDocumentMapper.to_document(inventory)),
                CreateDocument(CosmosDocumentMapper.to_document(transaction)),
                change_event_create(event),
            ]
            if media_values is not None:
                writes.append(
                    build_pending_media_retention_work_item(
                        household_id=record.household_id,
                        operation_id=adjustment.operation_id,
                        approval_id=record.approval_id,
                        image_ref=media_values[1],
                        created_at=record.decided_at,
                    )
                )
            operations_by_id[plan.operation_id] = tuple(writes)
            outcomes[plan.operation_id] = {
                "item_id": inventory.item_id,
                "transaction_id": transaction.transaction_id,
                "quantity_added": transaction.quantity_delta,
                "inventory_quantity": inventory.quantity,
                "unit": inventory.unit,
            }

        if selected_increase_operations:
            if inventory_reader is None:
                raise ShoppingPreviewError(
                    "Configure B1 Inventory reads before writing increase_stock lines."
                )
            if not isinstance(self._planner, ExistingInventoryAdjustmentPlanner):
                raise ShoppingPreviewError(
                    "Configure C1's existing Inventory adjustment planner before writing."
                )
            for operation in selected_increase_operations:
                values = operation.values
                item_id = _stored_text(values, "item_id")
                name = _stored_text(values, "name")
                unit = _stored_text(values, "unit")
                transaction_id = _stored_text(values, "transaction_id")
                quantity_added = _stored_integer(values, "quantity_added", minimum=1)
                quantity_before = _stored_integer(values, "inventory_quantity_before", minimum=0)
                stored_reason = values.get("reason")
                if stored_reason not in (None, TransactionReason.ADJUSTMENT.value):
                    raise ShoppingPreviewError(
                        "The reason for increase_stock is fixed to adjustment."
                    )
                reason = TransactionReason.ADJUSTMENT
                target = _stock_target(operation)
                if operation.operation_id in transaction_ids or item_id in item_ids:
                    raise ShoppingPreviewError(
                        "Stock operation and Inventory identities must be unique per card."
                    )
                transaction_ids.add(operation.operation_id)
                transaction_ids.add(transaction_id)
                item_ids.add(item_id)

                mapped = await inventory_reader.read_inventory_item_with_etag(
                    household_id=record.household_id,
                    item_id=item_id,
                )
                if (
                    mapped is None
                    or mapped.etag != target.etag
                    or mapped.document_id != target.record_id
                    or mapped.record.household_id != record.household_id
                    or mapped.record.item_id != item_id
                    or mapped.record.name != name
                    or mapped.record.unit != unit
                    or mapped.record.quantity != quantity_before
                ):
                    raise TargetChanged(target.record_id)

                plan = self._planner.plan_existing_inventory_adjustment(
                    household_id=record.household_id,
                    operation_id=operation.operation_id,
                    approval_id=record.approval_id,
                    transaction_id=transaction_id,
                    inventory=mapped.record,
                    expected_etag=target.etag,
                    quantity_added=quantity_added,
                    reason=reason,
                    recorded_at=record.decided_at,
                )
                self._validate_existing_plan(
                    record=record,
                    operation=operation,
                    inventory=mapped.record,
                    expected_etag=target.etag,
                    quantity_added=quantity_added,
                    reason=reason,
                    plan=plan,
                )
                update = plan.inventory_update
                transaction = plan.transaction
                event = plan.change_event
                operations_by_id[operation.operation_id] = (
                    CreateDocument(CosmosDocumentMapper.to_document(transaction)),
                    change_event_create(event),
                )
                shared_replacements[("inventory", item_id)] = ReplaceDocument(
                    CosmosDocumentMapper.to_document(update.record),
                    if_match_etag=update.expected_etag,
                )
                outcomes[operation.operation_id] = {
                    "item_id": item_id,
                    "transaction_id": transaction_id,
                    "quantity_added": quantity_added,
                    "inventory_quantity": update.record.quantity,
                    "unit": unit,
                }

        return DomainWritePlan(
            operations_by_id=operations_by_id,
            shared_operations=tuple(shared_replacements.values()),
            outcome=outcomes,
        )

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

    @staticmethod
    def _validate_existing_plan(
        *,
        record: ApprovalRecord,
        operation: StoredOperation,
        inventory: InventoryItem,
        expected_etag: str,
        quantity_added: int,
        reason: TransactionReason,
        plan: ExistingInventoryAdjustmentPlan,
    ) -> None:
        update = plan.inventory_update
        updated_inventory = update.record
        transaction = plan.transaction
        event = plan.change_event
        expected_inventory = inventory.model_copy(
            update={
                "quantity": inventory.quantity + quantity_added,
                "updated_at": record.decided_at,
            }
        )
        if (
            plan.household_id != record.household_id
            or plan.operation_id != operation.operation_id
            or plan.approval_id != record.approval_id
            or plan.recorded_at != record.decided_at
            or update.expected_etag != expected_etag
            or update.operation_ids != (operation.operation_id,)
            or updated_inventory != expected_inventory
            or transaction.household_id != record.household_id
            or transaction.operation_id != operation.operation_id
            or transaction.transaction_id != operation.values.get("transaction_id")
            or transaction.item_id != inventory.item_id
            or transaction.quantity_delta != quantity_added
            or transaction.reason is not reason
            or transaction.created_at != record.decided_at
            or event.event_id != operation.operation_id
            or event.household_id != record.household_id
            or event.operation_id != operation.operation_id
            or event.approval_id != record.approval_id
            or event.transaction_id != transaction.transaction_id
            or event.shopping_item_id is not None
            or event.item_id != inventory.item_id
            or event.occurred_at != record.decided_at
            or event.recorded_at != record.decided_at
        ):
            raise ShoppingPreviewError(
                "The existing Inventory adjustment plan does not match the approved card."
            )


def _photo_media_values(
    values: dict[str, JsonValue],
) -> tuple[str, str, str] | None:
    image_ref = values.get("image_ref")
    input_id = values.get("input_id")
    mapping_etag = values.get("mapping_etag")
    if image_ref is None and input_id is None and mapping_etag is None:
        return None
    if (
        not isinstance(input_id, str)
        or not input_id.strip()
        or not isinstance(image_ref, str)
        or not image_ref.strip()
        or not isinstance(mapping_etag, str)
        or not mapping_etag.strip()
    ):
        raise ShoppingPreviewError(
            "Photo attachments require stored image_ref, input_id and mapping_etag values."
        )
    return input_id, image_ref, mapping_etag


def _stock_target(operation: StoredOperation):
    if len(operation.targets) != 1:
        raise ShoppingPreviewError(
            "An increase_stock line must have exactly one Inventory target version."
        )
    target = operation.targets[0]
    item_id = _stored_text(operation.values, "item_id")
    if target.record_id != f"inventory_item:{item_id}" or not target.etag.strip():
        raise ShoppingPreviewError(
            "An increase_stock target must match its stored item_id and ETag."
        )
    return target


def _stored_text(values: dict[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ShoppingPreviewError(f"Stored {key} must be a non-empty string.")
    return value


def _stored_integer(values: dict[str, JsonValue], key: str, *, minimum: int) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ShoppingPreviewError(f"Stored {key} must be an integer >= {minimum}.")
    return value
