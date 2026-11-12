from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.agent import (
    AgentRequest,
    AgentResponse,
    AgentResult,
    ApprovalPresentation,
    PendingApproval,
    ProposedOperation,
    ResultKind,
    ResumeRequest,
)
from app.models.domain import InventoryItem, ShoppingItem, Transaction, TransactionReason

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def inventory_data():
    return dict(
        item_id="rice",
        household_id="demo",
        name="白米",
        quantity=2,
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
    with pytest.raises(ValidationError):
        InventoryItem(**(inventory_data() | {"quantity": 2.5}))


def test_shopping_quantity_and_unit_are_optional_without_default_quantity():
    item = ShoppingItem(
        shopping_item_id="milk",
        household_id="demo",
        name="牛奶",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    assert item.quantity is None
    assert item.unit is None


def test_shopping_quantity_must_be_integer_when_provided():
    with pytest.raises(ValidationError):
        ShoppingItem(**(shopping_data() | {"quantity": 0.5}))


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


@pytest.mark.parametrize("reason,delta", [("purchase", 2), ("consumption", -1), ("adjustment", -2)])
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


def test_transaction_quantity_delta_must_be_integer():
    with pytest.raises(ValidationError):
        Transaction(
            transaction_id="t",
            household_id="demo",
            item_id="rice",
            operation_id="stable-operation",
            quantity_delta=0.5,
            reason="purchase",
            created_at=NOW,
        )


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
                    action="add_to_shopping_list",
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


def test_item_match_and_new_item_cards_follow_issue_90():
    item_match = {
        "approval_id": "approval-1",
        "presentation": "item_match",
        "operations": [
            {
                "operation_id": "operation-1",
                "action": "confirm_item_match",
                "description": "這是家裡的洗潔精嗎？",
                "values": {
                    "user_term": "洗碗精",
                    "candidate_item_id": "item-dish-soap",
                    "candidate_name": "洗潔精",
                    "quantity": 1,
                    "unit": "瓶",
                },
            }
        ],
        "options": [
            {"option_id": "same_item", "label": "是，加到洗潔精"},
            {"option_id": "new_item", "label": "不是，這是新的東西"},
        ],
    }
    new_item = {
        "approval_id": "approval-2",
        "presentation": "single_action",
        "operations": [
            {
                "operation_id": "operation-2",
                "action": "create_item",
                "description": "會新增一個庫存品項：洗碗精",
                "values": {"name": "洗碗精", "quantity": 1, "unit": "瓶", "location": "廚房"},
            }
        ],
    }
    for card in (item_match, new_item):
        parsed = PendingApproval.model_validate(card)
        assert parsed.model_dump(mode="json") == card | {"options": card.get("options", [])}


@pytest.mark.parametrize("action", [None, ""])
def test_every_operation_says_what_it_does(action):
    fields = dict(operation_id="operation-1", description="Add milk", values={})
    if action is not None:
        fields["action"] = action
    with pytest.raises(ValidationError):
        ProposedOperation(**fields)


def test_agent_request_may_name_the_card_it_changes():
    """送出修改 on a card sends the change with the card's approval_id (Issue #91)."""
    change = AgentRequest(
        household_id="demo-household",
        message="衛生紙：數量改成 3，位置改成 浴室櫃",
        replaces_approval_id="approval-1",
    )
    assert change.replaces_approval_id == "approval-1"

    chat = AgentRequest(household_id="demo-household", message="家裡還有白米嗎？")
    assert chat.replaces_approval_id is None


def test_agent_request_rejects_a_blank_card_to_replace():
    with pytest.raises(ValidationError):
        AgentRequest(
            household_id="demo-household",
            message="衛生紙：數量改成 3",
            replaces_approval_id="",
        )


def resume_data(**changes):
    return dict(household_id="demo-household", approval_id="approval-1", approved=True) | changes


def test_resume_without_selection_fields_still_valid():
    resume = ResumeRequest(**resume_data())
    assert resume.approved_operation_ids is None
    assert resume.selected_option_id is None


def test_resume_carries_batch_and_purchase_selections():
    batch = ResumeRequest(**resume_data(approved_operation_ids=["op-101", "op-103"]))
    purchase = ResumeRequest(**resume_data(selected_option_id="option-001"))
    assert batch.approved_operation_ids == ["op-101", "op-103"]
    assert purchase.selected_option_id == "option-001"
    assert ResumeRequest.model_validate_json(batch.model_dump_json()) == batch


def test_resume_cancel_may_carry_ignored_selection_fields():
    resume = ResumeRequest(
        **resume_data(approved=False, approved_operation_ids=["op-101"], selected_option_id="o-1")
    )
    assert resume.approved is False


@pytest.mark.parametrize(
    "changes",
    [
        {"approved_operation_ids": []},
        {"approved_operation_ids": [""]},
        {"approved_operation_ids": ["  "]},
        {"approved_operation_ids": ["op-101", "op-101"]},
        {"selected_option_id": ""},
        {"selected_option_id": "  "},
    ],
)
def test_resume_rejects_empty_or_repeated_selections(changes):
    with pytest.raises(ValidationError):
        ResumeRequest(**resume_data(**changes))


@pytest.mark.parametrize("extra", ["values", "quantity", "selected_operation_ids"])
def test_resume_rejects_client_write_payloads(extra):
    with pytest.raises(ValidationError):
        ResumeRequest(**resume_data(**{extra: 1}))
