"""Issue #162: 送出修改 sent as data (`card_changes`); the model is not used.

Contract section 7. The new card is made from the stored card plus the changes, read
from the current records; the old card is retired as with any 送出修改. A refused
change is a 200 reply with `results[0].data.code`, the old card shown again while it
is still pending, and nothing new stored. Same faked Cosmos container and real
services as `test_purchase_cards`; the clock is T0 = 2026-11-10 16:00 Taipei.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError
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
)

from app.agent.cards import CARD_CHANGED, PurchaseCards
from app.agent.read_agent import ReadAgent, load_system_prompt
from app.models.agent import AgentRequest
from app.services.cosmos_read import CosmosInventoryService, CosmosShoppingService
from app.tools.queries import QueryTools

YESTERDAY_NOON = "2026-11-09T12:00:00+08:00"
TISSUE = {"shopping_item_id": "tissue-1", "quantity": 2}


def dated_agent(model, reader, approvals, dated=True):
    cards = PurchaseCards(reader, approvals, dated_purchases=dated, clock=lambda: T0)
    tools = QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader))
    return ReadAgent(model, tools, load_system_prompt(), purchases=cards)


def make_card(reader, approvals, *items, dated=True):
    model = ScriptedModel(purchase_reply(*items, call_id="call-1"), AIMessage(content="請確認"))
    return ask(dated_agent(model, reader, approvals, dated), "買回來了").pending_approval


def change(reader, approvals, old, *changes, dated=True):
    model = ScriptedModel()  # any model call would fail: there are no replies
    request = AgentRequest(
        household_id=HOME,
        message="送出修改",
        replaces_approval_id=old.approval_id,
        card_changes=list(changes),
    )
    response = asyncio.run(dated_agent(model, reader, approvals, dated).respond(request))
    assert model.seen == []
    return response


def cards(container):
    return {doc["approval_id"]: doc["status"] for doc in container.of_type("pending_approval")}


def code(response):
    return response.results[0].data["code"]


def test_a_quantity_change_makes_a_new_card_without_the_model():
    container, reader, approvals = setup()
    old = make_card(reader, approvals, TISSUE)
    (line,) = old.operations

    response = change(reader, approvals, old, {"operation_id": line.operation_id, "quantity": 1})

    new = response.pending_approval
    assert response.message == CARD_CHANGED
    assert response.results[0].data == {
        "code": "card_changed",
        "replaced_approval_id": old.approval_id,
    }
    assert [op.description for op in new.operations] == ["衛生紙 +1 包（從待購清單完成購買）"]
    assert cards(container) == {old.approval_id: "cancelled", new.approval_id: "pending"}
    stored = container.get(f"pending_approval:{new.approval_id}")
    assert stored["replaces_approval_id"] == old.approval_id

    body = {"household_id": HOME, "approval_id": new.approval_id, "approved": True}
    with TestClient(resume_app(approvals)) as client:
        written = client.post("/agent/resume", json=body)
    assert written.json()["message"] == "已記錄：衛生紙 +1 包（現在 2 包）"


def test_a_purchase_day_goes_on_the_new_card_and_null_takes_it_off():
    _, reader, approvals = setup()
    old = make_card(reader, approvals, TISSUE)
    (line,) = old.operations

    dated = change(
        reader, approvals, old, {"operation_id": line.operation_id, "purchased_on": "2026-11-09"}
    ).pending_approval

    (dated_line,) = dated.operations
    assert dated_line.values["purchased_at"] == YESTERDAY_NOON
    assert dated_line.values["purchased_quantity"] == 2  # left out, so unchanged
    assert dated_line.description == "衛生紙 +2 包（11/9 買的，從待購清單完成購買）"

    undated = change(
        reader, approvals, dated, {"operation_id": dated_line.operation_id, "purchased_on": None}
    ).pending_approval

    (undated_line,) = undated.operations
    assert "purchased_at" not in undated_line.values
    assert undated_line.description == "衛生紙 +2 包（從待購清單完成購買）"


def test_lines_left_out_of_the_change_keep_their_quantity_and_day():
    container, reader, approvals = setup(
        inventory(),
        shopping(),
        inventory(item_id="detergent", name="洗衣精", unit="瓶"),
        shopping("detergent-1", "洗衣精", "detergent", 2, "瓶"),
    )
    old = make_card(
        reader,
        approvals,
        {**TISSUE, "purchased_on": "2026-11-09"},
        {"shopping_item_id": "detergent-1", "quantity": 2},
    )
    tissue, detergent = old.operations

    new = change(
        reader, approvals, old, {"operation_id": detergent.operation_id, "quantity": 1}
    ).pending_approval

    new_tissue, new_detergent = new.operations
    assert new_tissue.values["purchased_quantity"] == 2
    assert new_tissue.values["purchased_at"] == YESTERDAY_NOON
    assert new_detergent.values["purchased_quantity"] == 1
    assert "purchased_at" not in new_detergent.values


@pytest.mark.parametrize(
    ("changes", "expected", "dated"),
    [
        ([{"operation_id": "nope", "quantity": 1}], "unknown_operation", True),
        ([{"operation_id": "LINE", "quantity": 1}] * 2, "duplicate_operation", True),
        ([{"operation_id": "LINE", "quantity": 2}], "nothing_changed", True),
        ([{"operation_id": "LINE", "purchased_on": "2026-11-09"}], "purchase_day_off", False),
        ([{"operation_id": "LINE", "purchased_on": "2026-11-11"}], "future_date", True),
        ([{"operation_id": "LINE", "quantity": 5}], "line_refused", True),  # listed: 2
    ],
)
def test_a_refused_change_stores_nothing_and_shows_the_old_card_again(changes, expected, dated):
    container, reader, approvals = setup()
    old = make_card(reader, approvals, TISSUE, dated=dated)
    (line,) = old.operations
    said = [
        {**c, "operation_id": line.operation_id} if c["operation_id"] == "LINE" else c
        for c in changes
    ]

    response = change(reader, approvals, old, *said, dated=dated)

    assert code(response) == expected
    assert response.pending_approval == old  # still on screen, same approval_id
    assert cards(container) == {old.approval_id: "pending"}


def test_the_refused_line_is_named_with_its_reason():
    _, reader, approvals = setup()
    old = make_card(reader, approvals, TISSUE)
    (line,) = old.operations

    response = change(reader, approvals, old, {"operation_id": line.operation_id, "quantity": 5})

    assert response.message == "沒有出新卡：「衛生紙」買的數量比待購清單上多，現在還不能記錄。"
    assert response.results[0].data["lines"] == [
        {
            "operation_id": line.operation_id,  # Issue #124: every refused line has it
            "shopping_item_id": "tissue-1",
            "reason": "買的數量比待購清單上多，現在還不能記錄。",
        }
    ]


def test_a_card_that_cannot_change_any_more_gets_no_card():
    container, reader, approvals = setup()
    old = make_card(reader, approvals, TISSUE)
    (line,) = old.operations
    body = {"household_id": HOME, "approval_id": old.approval_id, "approved": False}
    with TestClient(resume_app(approvals)) as client:
        client.post("/agent/resume", json=body)

    response = change(reader, approvals, old, {"operation_id": line.operation_id, "quantity": 1})

    assert code(response) == "card_not_changeable"
    assert response.results[0].data["reason"] == "cancelled"
    assert response.pending_approval is None
    assert cards(container) == {old.approval_id: "cancelled"}


@pytest.mark.parametrize(
    "fields",
    [
        {"card_changes": [{"operation_id": "op", "quantity": 1}]},  # no replaces_approval_id
        {"replaces_approval_id": "a", "card_changes": []},
        {"replaces_approval_id": "a", "card_changes": [{"operation_id": "op"}]},
        {"replaces_approval_id": "a", "card_changes": [{"operation_id": "op", "quantity": None}]},
        {"replaces_approval_id": "a", "card_changes": [{"operation_id": "op", "quantity": 0}]},
        {"replaces_approval_id": "a", "card_changes": [{"operation_id": "op", "quantity": "3"}]},
        {
            "replaces_approval_id": "a",
            "card_changes": [{"operation_id": "op", "purchased_on": "11/9"}],
        },
    ],
)
def test_a_malformed_change_is_rejected_before_anything_runs(fields):
    with pytest.raises(ValidationError):
        AgentRequest(household_id=HOME, message="送出修改", **fields)
