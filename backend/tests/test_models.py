from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.agent import (
    AgentResponse,
    AgentResult,
    ApprovalPresentation,
    PendingApproval,
    ProposedOperation,
    ResultKind,
)
from app.models.domain import InventoryItem, ShoppingItem, Transaction, TransactionReason

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def inventory_data():
    return dict(
        item_id="rice",
        household_id="demo",
        name="白米",
        quantity=2.5,
        unit="包",
        created_at=NOW,
        updated_at=NOW,
    )


def shopping_data():
    return dict(
        shopping_item_id="milk",
        household_id="demo",
        name="牛奶",
        quantity=1,
        unit="瓶",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )


def test_inventory_optional_metadata_and_json_roundtrip():
    item = InventoryItem(
        **inventory_data(),
        aliases=["米"],
        location="客廳左側櫃子",
        expiry_date="2027-01-01",
        image_ref="opaque-reference",
    )
    assert InventoryItem.model_validate_json(item.model_dump_json()) == item
    assert InventoryItem(**inventory_data()).image_ref is None


@pytest.mark.parametrize("extra", [{"category": "food"}, {"image_base64": "binary"}])
def test_inventory_rejects_unagreed_fields(extra):
    with pytest.raises(ValidationError):
        InventoryItem(**inventory_data(), **extra)


def test_quantity_cannot_be_reassigned_or_nonfinite():
    item = InventoryItem(**inventory_data())
    with pytest.raises(ValidationError):
        item.quantity = 0
    with pytest.raises(ValidationError):
        InventoryItem(**(inventory_data() | {"quantity": float("nan")}))


@pytest.mark.parametrize("status", ["pending", "completed"])
def test_shopping_status_and_external_reference(status):
    item = ShoppingItem(
        **(shopping_data() | {"status": status}),
        source="example",
        product_url="https://example.com/milk",
    )
    assert item.status == status


@pytest.mark.parametrize("changes", [{"price": 12}, {"status": "ordered"}])
def test_shopping_rejects_price_or_unagreed_status(changes):
    with pytest.raises(ValidationError):
        ShoppingItem(**(shopping_data() | changes))


@pytest.mark.parametrize(
    "reason,delta", [("purchase", 2), ("consumption", -1), ("adjustment", -0.5)]
)
def test_transaction_shared_semantics(reason, delta):
    transaction = Transaction(
        transaction_id="t",
        household_id="demo",
        item_id="rice",
        operation_id="stable-operation",
        quantity_delta=delta,
        reason=reason,
        created_at=NOW,
    )
    assert transaction.reason == TransactionReason(reason)
    assert transaction.quantity_delta == delta


@pytest.mark.parametrize("presentation", list(ApprovalPresentation))
def test_provisional_approval_and_result_concepts(presentation):
    response = AgentResponse(
        message="Preview only",
        results=[AgentResult(kind=kind, summary="Preview") for kind in ResultKind],
        pending_approval=PendingApproval(
            approval_id="approval-1",
            presentation=presentation,
            operations=[
                ProposedOperation(
                    operation_id="operation-1",
                    description="Add milk",
                    values={"name": "牛奶", "quantity": 1, "unit": "瓶"},
                )
            ],
        ),
    )
    assert AgentResponse.model_validate_json(response.model_dump_json()) == response


def test_empty_approval_is_invalid():
    with pytest.raises(ValidationError):
        PendingApproval(approval_id="empty", presentation="single_action", operations=[])
