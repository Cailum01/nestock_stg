import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from test_approvals_cosmos_write import FakeCosmosContainer

from app.core.dependencies import _create_approval_service
from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import MediaInputMapping, MediaProcessingStatus
from app.services.approvals import (
    ApprovalService,
    BatchTooLarge,
    NewOperation,
    ResumeOutcome,
)
from app.services.cosmos_read import CosmosReadRepository
from app.services.cosmos_write import CosmosWriteRepository
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.media_mapping import CosmosMediaMappingStorage
from app.services.photo_item_writes import PhotoItemWriteBuilder
from app.services.purchase_inputs import PhotoInputResolver
from app.services.shopping.planning import ShoppingPlanService
from app.services.shopping.preview import ShoppingPreviewError

HOME = "test-household"
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)


class FakeMediaCosmosContainer(FakeCosmosContainer):
    async def query_items(self, *, query, parameters, partition_key):
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        if values.get("@record_type") == "user_input":
            return [
                dict(document)
                for (household_id, _), document in self.documents.items()
                if household_id == partition_key == values["@household_id"]
                and document.get("record_type") == "user_input"
                and document.get("image_ref") == values["@image_ref"]
            ]
        return await super().query_items(
            query=query,
            parameters=parameters,
            partition_key=partition_key,
        )


def run(coro):
    return asyncio.run(coro)


def new_item(
    name: str,
    transaction_id: str,
    *,
    media_values: dict[str, object] | None = None,
) -> NewOperation:
    values = {
        "name": name,
        "quantity": 3,
        "unit": "個",
        "location": None,
        "transaction_id": transaction_id,
    }
    if media_values is not None:
        values.update(media_values)
    return NewOperation(
        action="create_item",
        description=f"建立庫存品項：{name}",
        values=values,
    )


def setup(*, batch: bool = False, resolver=None, planner=None, item_count=None, save=True):
    container = FakeCosmosContainer()
    repository = CosmosWriteRepository(container, query_client=container)
    count = item_count if item_count is not None else (2 if batch else 1)
    ids = iter(["approval-1", *(f"operation-{index}" for index in range(1, count + 1))])
    if resolver is None and planner is None:
        service = _create_approval_service(CosmosReadRepository(container), repository, container)
        assert service is not None
    else:
        service = ApprovalService(
            repository,
            PhotoItemWriteBuilder(resolver or PhotoInputResolver(), planner=planner),
            clock=lambda: T0,
            id_factory=lambda: next(ids),
        )
    names = ["羅技 G400s 有線電競滑鼠", "手作收納零件 7B"]
    operations = [
        new_item(
            names[index - 1] if index <= len(names) else f"測試品項 {index}",
            f"transaction-{index}",
        )
        for index in range(1, count + 1)
    ]
    record = service.prepare(
        household_id=HOME,
        presentation=(
            ApprovalPresentation.BATCH_ACTION if count > 1 else ApprovalPresentation.SINGLE_ACTION
        ),
        operations=operations,
    )
    saved = run(service.save(record)) if save else record
    return service, container, saved


def confirm(service, record, *, operation_ids=None):
    return run(
        service.resume(
            ResumeRequest(
                household_id=record.household_id,
                approval_id=record.approval_id,
                approved=True,
                approved_operation_ids=operation_ids,
            )
        )
    )


def create_completed_mapping(container, *, input_id: str, image_ref: str):
    storage = CosmosMediaMappingStorage(
        read_repository=CosmosReadRepository(container),
        write_client=container,
    )
    mapping = MediaInputMapping(
        input_id=input_id,
        household_id=HOME,
        image_ref=image_ref,
        content_type="image/jpeg",
        expected_file_size_bytes=100,
        status=MediaProcessingStatus.ALLOCATED,
        upload_authorization_expires_at=T0 + timedelta(minutes=5),
        created_at=T0,
        updated_at=T0,
    )
    snapshot = run(storage.create_mapping(mapping, orphan_ttl_seconds=86_400))
    snapshot = run(
        storage.transition(
            household_id=HOME,
            input_id=input_id,
            expected_etag=snapshot.etag,
            status=MediaProcessingStatus.RECEIVED,
            updated_at=T0,
            file_size_bytes=100,
            sha256_hash="a" * 64,
        )
    )
    snapshot = run(
        storage.transition(
            household_id=HOME,
            input_id=input_id,
            expected_etag=snapshot.etag,
            status=MediaProcessingStatus.PROCESSING,
            updated_at=T0,
        )
    )
    snapshot = run(
        storage.transition(
            household_id=HOME,
            input_id=input_id,
            expected_etag=snapshot.etag,
            status=MediaProcessingStatus.COMPLETED,
            updated_at=T0,
            artifact_ref="opaque-artifact",
        )
    )
    return storage, snapshot


def test_confirm_creates_new_inventory_transaction_and_unlinked_change_event_atomically():
    service, container, card = setup()

    assert container.of_type("inventory_item") == []
    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.COMPLETED
    assert result.record is not None
    (domain_batch,) = [
        batch
        for batch in container.batches
        if any(operation.document.get("record_type") == "operation_receipt" for operation in batch)
    ]
    assert len(domain_batch) == 5
    assert {operation.document["record_type"] for operation in domain_batch} == {
        "inventory_item",
        "transaction",
        "change_event",
        "operation_receipt",
        "pending_approval",
    }
    inventory = container.of_type("inventory_item")
    transaction = container.of_type("transaction")
    event = container.of_type("change_event")
    assert len(inventory) == len(transaction) == len(event) == 1
    assert inventory[0]["name"] == "羅技 G400s 有線電競滑鼠"
    assert inventory[0]["quantity"] == transaction[0]["quantity_delta"] == 3
    assert transaction[0]["reason"] == "adjustment"
    assert result.record.outcome == {
        card.operation_ids[0]: {
            "item_id": inventory[0]["item_id"],
            "transaction_id": transaction[0]["transaction_id"],
            "quantity_added": transaction[0]["quantity_delta"],
            "inventory_quantity": inventory[0]["quantity"],
            "unit": inventory[0]["unit"],
        }
    }
    assert event[0]["shopping_item_id"] is None
    assert event[0]["item_id"] == inventory[0]["item_id"]
    assert event[0]["operation_id"] == card.operation_ids[0]
    assert event[0]["occurred_at"] == event[0]["recorded_at"]
    assert container.of_type("shopping_item") == []
    saved_inventory = run(
        CosmosReadRepository(container).read_inventory_item(
            household_id=HOME,
            item_id=inventory[0]["item_id"],
        )
    )
    assert saved_inventory is not None
    assert saved_inventory.name == inventory[0]["name"]


def test_confirm_attaches_photo_and_removes_mapping_ttl_in_same_batch():
    container = FakeMediaCosmosContainer()
    repository = CosmosWriteRepository(container, query_client=container)
    input_id, image_ref = "photo-input-1", "opaque-photo-1"
    mapping_storage, snapshot = create_completed_mapping(
        container,
        input_id=input_id,
        image_ref=image_ref,
    )
    ids = iter(["approval-photo", "operation-photo"])
    service = ApprovalService(
        repository,
        PhotoItemWriteBuilder(
            PhotoInputResolver(),
            media_mapping_storage=mapping_storage,
        ),
        clock=lambda: T0,
        id_factory=lambda: next(ids),
    )
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[
            new_item(
                "洗碗精",
                "transaction-photo",
                media_values={
                    "image_ref": image_ref,
                    "input_id": input_id,
                    "mapping_etag": snapshot.etag,
                },
            )
        ],
    )
    card = run(service.save(record))

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.COMPLETED
    inventory = container.of_type("inventory_item")
    assert len(inventory) == 1
    assert inventory[0]["image_ref"] == image_ref
    domain_batch = next(
        batch
        for batch in container.batches
        if any(operation.document.get("record_type") == "operation_receipt" for operation in batch)
    )
    mapping_update = next(
        operation
        for operation in domain_batch
        if operation.document.get("id") == f"input:{input_id}"
    )
    assert mapping_update.if_match_etag == snapshot.etag
    assert "ttl" not in mapping_update.document
    assert len(domain_batch) == 6
    retained = run(mapping_storage.find_by_image_ref(household_id=HOME, image_ref=image_ref))
    assert retained is not None
    assert retained.record.ttl_seconds is None

    batch_count = len(container.batches)
    retry = confirm(service, card)
    assert retry.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert len(container.batches) == batch_count


@pytest.mark.parametrize("mapping_change", ["etag", "missing"])
def test_changed_or_missing_photo_mapping_prevents_all_domain_writes(mapping_change):
    container = FakeMediaCosmosContainer()
    repository = CosmosWriteRepository(container, query_client=container)
    input_id, image_ref = "photo-input-2", "opaque-photo-2"
    mapping_storage, snapshot = create_completed_mapping(
        container,
        input_id=input_id,
        image_ref=image_ref,
    )
    ids = iter(["approval-stale-photo", "operation-stale-photo"])
    service = ApprovalService(
        repository,
        PhotoItemWriteBuilder(
            PhotoInputResolver(),
            media_mapping_storage=mapping_storage,
        ),
        clock=lambda: T0,
        id_factory=lambda: next(ids),
    )
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[
            new_item(
                "洗碗精",
                "transaction-stale-photo",
                media_values={
                    "image_ref": image_ref,
                    "input_id": input_id,
                    "mapping_etag": snapshot.etag,
                },
            )
        ],
    )
    card = run(service.save(record))
    if mapping_change == "etag":
        mapping_document = dict(container.get(f"input:{input_id}", HOME))
        container.put(mapping_document)
    else:
        container.documents.pop((HOME, f"input:{input_id}"))

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    assert container.of_type("inventory_item") == []
    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []
    retained = run(mapping_storage.find_by_image_ref(household_id=HOME, image_ref=image_ref))
    if mapping_change == "etag":
        assert retained is not None
        assert retained.record.ttl_seconds == 86_400
    else:
        assert retained is None


def test_partial_photo_mapping_metadata_is_rejected_before_card_is_saved():
    container = FakeCosmosContainer()
    repository = CosmosWriteRepository(container, query_client=container)
    ids = iter(["approval-partial-photo", "operation-partial-photo"])
    service = ApprovalService(
        repository,
        PhotoItemWriteBuilder(PhotoInputResolver()),
        clock=lambda: T0,
        id_factory=lambda: next(ids),
    )
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[
            new_item(
                "洗碗精",
                "transaction-partial-photo",
                media_values={"image_ref": "opaque-photo-partial"},
            )
        ],
    )

    with pytest.raises(ShoppingPreviewError):
        run(service.save(record))

    assert container.of_type("approval_card") == []


def test_batch_confirmation_writes_only_selected_new_items():
    service, container, card = setup(batch=True)

    result = confirm(service, card, operation_ids=[card.operation_ids[0]])

    assert result.outcome is ResumeOutcome.COMPLETED
    assert [item["name"] for item in container.of_type("inventory_item")] == [
        "羅技 G400s 有線電競滑鼠"
    ]
    assert len(container.of_type("transaction")) == 1
    assert len(container.of_type("change_event")) == 1
    assert container.of_type("shopping_item") == []


def test_batch_confirmation_writes_all_selected_items_in_one_transactional_batch():
    service, container, card = setup(batch=True)

    result = confirm(service, card, operation_ids=card.operation_ids)

    assert result.outcome is ResumeOutcome.COMPLETED
    domain_batches = [
        batch
        for batch in container.batches
        if any(operation.document.get("record_type") == "operation_receipt" for operation in batch)
    ]
    assert len(domain_batches) == 1
    assert len(domain_batches[0]) == 9
    assert len(container.of_type("inventory_item")) == 2
    assert len(container.of_type("transaction")) == 2
    assert len(container.of_type("change_event")) == 2
    assert len(container.of_type("operation_receipt")) == 2
    assert container.of_type("shopping_item") == []


def test_repeated_confirmation_does_not_duplicate_new_item_or_event():
    service, container, card = setup()
    request = ResumeRequest(
        household_id=card.household_id,
        approval_id=card.approval_id,
        approved=True,
    )

    first = run(service.resume(request))
    retry = run(service.resume(request))

    assert first.outcome is ResumeOutcome.COMPLETED
    assert retry.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert len(container.of_type("inventory_item")) == 1
    assert len(container.of_type("transaction")) == 1
    assert len(container.of_type("change_event")) == 1


def test_lost_batch_response_is_read_back_without_duplicate_writes():
    service, container, card = setup()
    container.lose_next_domain_batch_response = True

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert len(container.of_type("inventory_item")) == 1
    assert len(container.of_type("transaction")) == 1
    assert len(container.of_type("change_event")) == 1
    assert len(container.of_type("operation_receipt")) == 1
    assert (
        sum(
            any(operation.document.get("record_type") == "operation_receipt" for operation in batch)
            for batch in container.batches
        )
        == 1
    )


def test_existing_deterministic_item_id_is_reported_as_new_card_needed():
    service, container, card = setup()
    item_id = resolve_inventory_item_id(card.operation_ids[0])
    container.put(
        {
            "id": f"inventory_item:{item_id}",
            "record_type": "inventory_item",
            "household_id": HOME,
            "item_id": item_id,
            "name": "既有品項",
            "quantity": 1,
            "unit": "個",
        }
    )

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    assert len(container.of_type("inventory_item")) == 1
    assert container.of_type("inventory_item")[0]["name"] == "既有品項"
    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []
    assert container.of_type("operation_receipt") == []


@pytest.mark.parametrize(("item_count", "too_large"), [(24, False), (25, True)])
def test_photo_card_preflight_respects_cosmos_batch_limit(item_count, too_large):
    service, container, card = setup(item_count=item_count, save=False)

    if too_large:
        with pytest.raises(BatchTooLarge):
            run(service.save(card))
        assert container.of_type("pending_approval") == []
    else:
        saved = run(service.save(card))
        assert saved.status.value == "pending"
        assert len(container.of_type("pending_approval")) == 1
    assert container.of_type("inventory_item") == []
    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []


def test_planner_cannot_change_inventory_values_approved_on_the_card():
    class ChangedNamePlanner:
        def plan_adjustment(self, **kwargs):
            plan = ShoppingPlanService().plan_adjustment(**kwargs)
            item = plan.inventory_create.record.model_copy(update={"name": "未核准名稱"})
            return replace(plan, inventory_create=replace(plan.inventory_create, record=item))

    service, container, card = setup(planner=ChangedNamePlanner(), save=False)

    with pytest.raises(ShoppingPreviewError, match="adjustment plan does not match"):
        run(service.save(card))

    assert container.of_type("pending_approval") == []
    assert container.of_type("inventory_item") == []


def test_planner_must_return_an_unlinked_change_event_for_new_item_card():
    class LinkedEventPlanner:
        def plan_adjustment(self, **kwargs):
            plan = ShoppingPlanService().plan_adjustment(**kwargs)
            event = plan.change_event.model_copy(update={"shopping_item_id": "unexpected"})
            return replace(plan, change_event=event)

    service, container, card = setup(planner=LinkedEventPlanner(), save=False)

    with pytest.raises(ShoppingPreviewError, match="adjustment plan does not match"):
        run(service.save(card))

    assert container.of_type("pending_approval") == []
    assert container.of_type("inventory_item") == []


def test_cancelled_photo_card_writes_no_domain_records():
    service, container, card = setup()

    result = run(
        service.resume(
            ResumeRequest(
                household_id=card.household_id,
                approval_id=card.approval_id,
                approved=False,
            )
        )
    )

    assert result.outcome is ResumeOutcome.CANCELLED
    assert container.of_type("inventory_item") == []
    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []
    assert container.of_type("operation_receipt") == []


def test_resolver_cannot_change_values_approved_on_the_stored_card():
    class ChangedQuantityResolver:
        def resolve(self, record):
            return tuple(
                replace(adjustment, quantity=adjustment.quantity + 1)
                for adjustment in PhotoInputResolver().resolve(record)
            )

    container = FakeCosmosContainer()
    repository = CosmosWriteRepository(container, query_client=container)
    service = ApprovalService(
        repository,
        PhotoItemWriteBuilder(ChangedQuantityResolver()),
        clock=lambda: T0,
        id_factory=iter(["approval-1", "operation-1"]).__next__,
    )
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[new_item("自訂品項", "transaction-1")],
    )

    with pytest.raises(ShoppingPreviewError, match="match the stored approved card"):
        run(service.save(record))

    assert container.of_type("pending_approval") == []
    assert container.of_type("inventory_item") == []
