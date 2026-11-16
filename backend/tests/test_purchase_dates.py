"""A1-50: a purchase day the user says (「昨天買的」) on the purchase card.

Built but off in the running app (`PurchaseCards(dated_purchases=False)`): the App
shows only name, quantity and unit on a purchase card, and a card may only hold values
the user sees (HITL rule 4). Contract section 11. Same faked Cosmos container and real
services as `test_purchase_cards`; the clock is T0 = 2026-11-10 16:00 Taipei.
"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage
from test_purchase_cards import (
    HOME,
    T0,
    ScriptedModel,
    ask,
    inventory,
    purchase_reply,
    resume_app,
    setup,
    shopping,
    tool_results,
)

from app.agent.cards import (
    FUTURE_DAY,
    PURCHASE_TOOL_SCHEMA,
    UNREADABLE_DAY,
    PurchaseCards,
)
from app.agent.read_agent import ReadAgent, load_system_prompt
from app.services.cosmos_read import CosmosInventoryService, CosmosShoppingService
from app.tools.queries import QueryTools

YESTERDAY_NOON = "2026-11-09T12:00:00+08:00"


def dated_agent(model, reader, approvals, dated=True):
    cards = PurchaseCards(reader, approvals, dated_purchases=dated, clock=lambda: T0)
    tools = QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader))
    return ReadAgent(model, tools, load_system_prompt(), purchases=cards)


def card_for(reader, approvals, item, dated=True, message="衛生紙昨天買的，2 包"):
    model = ScriptedModel(
        purchase_reply(item, call_id="call-1"), AIMessage(content="請確認下方的卡片。")
    )
    return ask(dated_agent(model, reader, approvals, dated), message), model


def offered_item_fields(model):
    (purchase,) = [t for t in model.bound_tools if t["function"]["name"] == "complete_purchase"]
    return purchase["function"]["parameters"]["properties"]["items"]["items"]["properties"]


def notes(model):
    return [m.content for m in model.seen[0] if isinstance(m, SystemMessage)]


def test_off_the_model_gets_no_day_field_and_no_date():
    _, reader, approvals = setup()

    response, model = card_for(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": "2026-11-09"},
        dated=False,
    )

    assert "purchased_on" not in offered_item_fields(model)
    assert PurchaseCards(reader, approvals).tool_schema is PURCHASE_TOOL_SCHEMA
    assert not any("今天是" in note for note in notes(model))
    (line,) = response.pending_approval.operations
    assert "purchased_at" not in line.values  # a day sent anyway is ignored
    assert line.description == "衛生紙 +2 包（從待購清單完成購買）"


def test_on_the_model_gets_the_day_field_and_today_in_taipei():
    _, reader, approvals = setup()

    _, model = card_for(reader, approvals, {"shopping_item_id": "tissue-1", "quantity": 2})

    assert "purchased_on" in offered_item_fields(model)
    assert any("今天是 2026-11-10（台北時間）" in note for note in notes(model))


def test_yesterday_goes_on_the_line_as_noon_taipei_and_in_its_text():
    container, reader, approvals = setup()

    response, model = card_for(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": "2026-11-09"},
    )

    (line,) = response.pending_approval.operations
    assert line.values["purchased_at"] == YESTERDAY_NOON
    assert line.description == "衛生紙 +2 包（11/9 買的，從待購清單完成購買）"
    assert json.loads(tool_results(model)[-1])["card"]["lines"] == [line.description]
    assert container.of_type("transaction") == []  # still only a pending card


@pytest.mark.parametrize("day", ["2026-11-10", "", None])
def test_today_or_no_day_uses_the_confirmation_time(day):
    _, reader, approvals = setup()
    item = {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": day}

    response, _ = card_for(reader, approvals, {k: v for k, v in item.items() if v is not None})

    (line,) = response.pending_approval.operations
    assert "purchased_at" not in line.values
    assert line.description == "衛生紙 +2 包（從待購清單完成購買）"


@pytest.mark.parametrize(("day", "reason"), [("2026-11-11", FUTURE_DAY), ("昨天", UNREADABLE_DAY)])
def test_a_future_or_unreadable_day_leaves_the_item_off(day, reason):
    container, reader, approvals = setup()

    response, model = card_for(
        reader, approvals, {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": day}
    )

    assert response.pending_approval is None
    assert json.loads(tool_results(model)[-1])["not_on_card"] == [
        {"shopping_item_id": "tissue-1", "reason": reason}
    ]
    assert container.of_type("pending_approval") == []


def test_a_new_item_line_carries_the_day_too():
    _, reader, approvals = setup(inventory(), shopping("paste-1", "牙膏", None, 1, "條"))

    response, _ = card_for(
        reader,
        approvals,
        {
            "shopping_item_id": "paste-1",
            "quantity": 1,
            "location": "浴室櫃子",
            "purchased_on": "2026-11-09",
        },
        message="牙膏昨天買的，1 條，放浴室櫃子",
    )

    (line,) = response.pending_approval.operations
    assert line.values["purchased_at"] == YESTERDAY_NOON
    assert line.description == (
        "會新增一個庫存品項：牙膏 1 條，放在浴室櫃子（11/9 買的，從待購清單完成購買）"
    )


def test_confirming_a_dated_card_dates_the_transaction_and_event():
    container, reader, approvals = setup()
    response, _ = card_for(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": "2026-11-09"},
    )
    card = response.pending_approval
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": True}

    with TestClient(resume_app(approvals)) as client:
        first = client.post("/agent/resume", json=body)
        again = client.post("/agent/resume", json=body)  # a retry keeps the same time

    assert first.status_code == 200
    assert again.json()["message"] == first.json()["message"]
    (transaction,) = container.of_type("transaction")
    (event,) = container.of_type("change_event")
    noon = datetime.fromisoformat(YESTERDAY_NOON)
    assert datetime.fromisoformat(transaction["created_at"]) == noon
    assert datetime.fromisoformat(event["occurred_at"]) == noon
    assert datetime.fromisoformat(event["recorded_at"]) == T0  # when the user confirmed
    assert container.get("inventory_item:tissue")["quantity"] == 3
