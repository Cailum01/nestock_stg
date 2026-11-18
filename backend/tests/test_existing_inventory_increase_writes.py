import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from test_approvals_cosmos_write import FakeCosmosContainer

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import InventoryItem, TransactionReason
from app.services.approvals import (
    ApprovalService,
    NewOperation,
    ResumeOutcome,
    TargetVersion,
)
from app.services.card_writes import CardWriteRouter
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_write import CosmosWriteRepository
from app.services.photo_item_writes import PhotoItemWriteBuilder
from app.services.purchase_inputs import PhotoInputResolver
from app.services.shopping.planning import ShoppingPlanService
from app.services.shopping.preview import ShoppingPreviewError

HOME = "stock-increase-household"
ITEM_ID = "dish-soap"
T0 = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)


class UnexpectedPurchaseBuilder:
    async def build(self, record):
        raise AssertionError("An increase_stock card must use the Inventory writer.")


def _setup(*, reason="adjustment", mixed=False, save=True):
    container = FakeCosmosContainer()
    item = InventoryItem(
        item_id=ITEM_ID,
        household_id=HOME,
        name="洗碗精",
        quantity=3,
        unit="瓶",
        created_at=T0 - timedelta(days=3),
        updated_at=T0 - timedelta(days=1),
    )
    etag = container.put(CosmosDocumentMapper.to_document(item))
    reader = CosmosReadRepository(container)
    operations = []
    ids = ["approval-1"]

    if mixed:
        ids.append("create-operation")
        operations.append(
            NewOperation(
                action="create_item",
                description="新增垃圾袋",
                values={
                    "name": "垃圾袋",
                    "quantity": 2,
                    "unit": "包",
                    "location": None,
                    "transaction_id": "create-transaction",
                },
            )
        )

    ids.append("increase-operation")
    increase_values = {
        "item_id": ITEM_ID,
        "name": item.name,
        "quantity_added": 2,
        "inventory_quantity_before": item.quantity,
        "unit": item.unit,
        "transaction_id": "increase-transaction",
    }
    if reason is not None:
        increase_values["reason"] = reason
    operations.append(
        NewOperation(
            action="increase_stock",
            description="洗碗精 +2 瓶",
            values=increase_values,
            targets=[TargetVersion(record_id=f"inventory_item:{ITEM_ID}", etag=etag)],
        )
    )
    builder = PhotoItemWriteBuilder(PhotoInputResolver(), planner=ShoppingPlanService())
    router = CardWriteRouter(
        purchases=UnexpectedPurchaseBuilder(),
        photo_items=builder,
        household_items=reader,
    )
    service = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        router,
        clock=lambda: T0,
        id_factory=iter(ids).__next__,
    )
    card = service.prepare(
        household_id=HOME,
        presentation=(
            ApprovalPresentation.BATCH_ACTION if mixed else ApprovalPresentation.SINGLE_ACTION
        ),
        operations=operations,
    )
    return service, container, item, run(service.save(card)) if save else card


def run(coro):
    return asyncio.run(coro)


def confirm(service, card):
    return run(
        service.resume(
            ResumeRequest(
                household_id=card.household_id,
                approval_id=card.approval_id,
                approved=True,
                approved_operation_ids=card.operation_ids
                if card.presentation is ApprovalPresentation.BATCH_ACTION
                else None,
            )
        )
    )


@pytest.mark.parametrize("reason", [TransactionReason.ADJUSTMENT.value, None])
def test_increase_stock_uses_fixed_adjustment_reason_and_writes_atomically(reason):
    service, container, item, card = _setup(reason=reason)

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.COMPLETED
    (stored_item,) = container.of_type("inventory_item")
    assert stored_item["quantity"] == item.quantity + 2
    (transaction,) = container.of_type("transaction")
    assert transaction["quantity_delta"] == 2
    assert transaction["reason"] == TransactionReason.ADJUSTMENT.value
    (event,) = container.of_type("change_event")
    assert event["item_id"] == item.item_id
    assert event["shopping_item_id"] is None
    assert len(container.of_type("operation_receipt")) == 1
    batch = next(
        batch
        for batch in container.batches
        if any(op.document.get("record_type") == "operation_receipt" for op in batch)
    )
    assert {op.document["record_type"] for op in batch} == {
        "operation_receipt",
        "transaction",
        "change_event",
        "inventory_item",
        "pending_approval",
    }


def test_stale_card_etag_writes_no_inventory_transaction_or_event():
    service, container, item, card = _setup()
    container.put(
        CosmosDocumentMapper.to_document(item.model_copy(update={"quantity": 9, "updated_at": T0}))
    )

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    (stored_item,) = container.of_type("inventory_item")
    assert stored_item["quantity"] == 9
    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []
    assert container.of_type("operation_receipt") == []


def test_etag_conflict_during_batch_keeps_all_domain_writes_atomic():
    service, container, item, card = _setup()

    def concurrent_update():
        container.put(
            CosmosDocumentMapper.to_document(
                item.model_copy(update={"quantity": 4, "updated_at": T0})
            )
        )

    container.before_next_domain_batch = concurrent_update
    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    (stored_item,) = container.of_type("inventory_item")
    assert stored_item["quantity"] == 4
    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []
    assert container.of_type("operation_receipt") == []


def test_same_confirmation_retry_does_not_increment_stock_twice():
    service, container, item, card = _setup()
    container.lose_next_domain_batch_response = True

    first = confirm(service, card)
    retry = confirm(service, card)

    assert first.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert retry.outcome is ResumeOutcome.ALREADY_COMPLETED
    (stored_item,) = container.of_type("inventory_item")
    assert stored_item["quantity"] == item.quantity + 2
    assert len(container.of_type("transaction")) == 1
    assert len(container.of_type("change_event")) == 1
    assert len(container.of_type("operation_receipt")) == 1


def test_mixed_new_and_existing_stock_lines_commit_in_one_batch():
    service, container, item, card = _setup(mixed=True)

    result = confirm(service, card)

    assert result.outcome is ResumeOutcome.COMPLETED
    assert {doc["name"]: doc["quantity"] for doc in container.of_type("inventory_item")} == {
        "洗碗精": item.quantity + 2,
        "垃圾袋": 2,
    }
    assert len(container.of_type("transaction")) == 2
    assert len(container.of_type("change_event")) == 2
    assert len(container.of_type("operation_receipt")) == 2
    domain_batch = next(
        batch
        for batch in container.batches
        if any(op.document.get("record_type") == "operation_receipt" for op in batch)
    )
    assert sum(op.document.get("record_type") == "inventory_item" for op in domain_batch) == 2


@pytest.mark.parametrize("reason", ["purchase", "consumption", ""])
def test_increase_stock_rejects_a_non_adjustment_reason(reason):
    service, container, _, card = _setup(reason=reason, save=False)

    with pytest.raises(ShoppingPreviewError, match="reason"):
        run(service.save(card))

    assert container.of_type("transaction") == []
    assert container.of_type("change_event") == []
    assert container.of_type("operation_receipt") == []
