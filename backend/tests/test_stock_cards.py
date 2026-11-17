"""A1 `add_stock`: a new item the user says they got, from the card to the record (Issue #168).

「我收到一瓶洗髮精」: the Agent's second card tool stores one pending new-item card and
writes nothing else; `/agent/resume` writes it through the app's own composition
(`_create_approval_service`): A1's `CardWriteRouter`, B1's no-Shopping writer and C1's
`plan_adjustment`, the same path as a photo new-item card. The model is scripted.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_photo_end_to_end import _app_approvals, _confirm
from test_purchase_cards import (
    HOME,
    FakeCosmosContainer,
    ScriptedModel,
    _call,
    inventory,
    resume_app,
    shopping,
    tool_results,
)

from app.agent.cards import CARD_CHANGED, RESUME_MESSAGES, SAID, PhotoCards, PurchaseCards
from app.agent.read_agent import ReadAgent, load_system_prompt
from app.agent.stock_cards import (
    ADD_STOCK,
    HAVE_IT,
    SAID_TWICE,
    STOCK_CARD_MADE,
    StockCards,
)
from app.models.agent import AgentRequest, ApprovalPresentation, CardChange
from app.services.approvals import ApprovalRecord, ResumeOutcome
from app.services.card_writes import CardWriteRouter
from app.services.cosmos_read import CosmosInventoryService, CosmosShoppingService
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.tools.queries import QueryTools

SHAMPOO = {"name": "洗髮精", "quantity": 1, "unit": "瓶", "location": "浴室"}


def home(*records):
    container = FakeCosmosContainer()
    for record in records or (inventory(), shopping()):
        container.put(record)
    reads, approvals = _app_approvals(container)
    return container, reads, approvals


def agent(reads, approvals, *replies, stock=True):
    return ReadAgent(
        ScriptedModel(*replies),
        QueryTools(CosmosInventoryService(reads), CosmosShoppingService(reads)),
        load_system_prompt(),
        purchases=PurchaseCards(reads, approvals),
        photo_cards=PhotoCards(approvals),
        stock=StockCards(reads, approvals) if stock else None,
    )


def said(reads, approvals, *items, message="朋友送我一瓶洗髮精，放在浴室"):
    the_agent = agent(
        reads,
        approvals,
        _call(ADD_STOCK, "call-1", items=list(items)),
        AIMessage(content="請確認下方的卡片，按確認後才會記錄。"),
    )
    response = asyncio.run(the_agent.respond(AgentRequest(household_id=HOME, message=message)))
    return response, the_agent._model


def items_named(container, name):
    return [d for d in container.of_type("inventory_item") if d["name"] == name]


def test_a_said_new_item_gets_a_card_and_nothing_is_written():
    container, reads, approvals = home()

    response, model = said(reads, approvals, SHAMPOO)

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.SINGLE_ACTION
    (line,) = card.operations
    assert line.action == "create_item"
    assert line.description == f"會新增一個庫存品項：洗髮精 1 瓶，放在浴室{SAID}"
    assert {k: v for k, v in line.values.items() if k != "transaction_id"} == {
        "name": "洗髮精",
        "quantity": 1,
        "unit": "瓶",
        "location": "浴室",
    }
    assert line.values["transaction_id"]
    stored = container.get(f"pending_approval:{card.approval_id}")
    assert stored["status"] == "pending"
    assert stored["operations"][0]["targets"] == []
    assert items_named(container, "洗髮精") == []
    assert container.of_type("transaction") == []
    result = json.loads(tool_results(model)[-1])
    assert result["card"] == {"lines": [line.description]}
    assert result["note"] == STOCK_CARD_MADE


def test_confirming_writes_the_new_item_once_as_an_adjustment():
    container, reads, approvals = home()
    shopping_before = container.of_type("shopping_item")
    card = said(reads, approvals, SHAMPOO)[0].pending_approval

    reply = _confirm(approvals, card)
    again = _confirm(approvals, card)  # a double tap writes once

    assert reply["message"] == "已記錄：洗髮精 +1 瓶（現在 1 瓶）"
    assert again["message"] == reply["message"]
    (item,) = items_named(container, "洗髮精")
    assert (item["quantity"], item["unit"], item["location"]) == (1, "瓶", "浴室")
    (tx,) = [d for d in container.of_type("transaction") if d["item_id"] == item["item_id"]]
    assert (tx["reason"], tx["quantity_delta"]) == ("adjustment", 1)
    assert tx["transaction_id"] == card.operations[0].values["transaction_id"]
    (event,) = [d for d in container.of_type("change_event") if d["item_id"] == item["item_id"]]
    assert event["shopping_item_id"] is None
    assert container.of_type("shopping_item") == shopping_before  # no Shopping row


@pytest.mark.parametrize(
    ("name", "aliases"), [("洗髮精", None), ("洗髮乳", ["洗髮精"])], ids=["same name", "alias"]
)
def test_an_item_added_after_the_card_is_not_added_twice(name, aliases):
    """PR #187 B1 review: the card is checked when made, and again when confirmed.

    The same item is added another way between the card and the confirm: nothing is
    written, the card is cancelled and the user is asked to say it again.
    """
    container, reads, approvals = home()
    card = said(reads, approvals, SHAMPOO)[0].pending_approval
    container.put(inventory(item_id="added", name=name, unit="瓶", aliases=aliases))
    items_before = container.of_type("inventory_item")
    transactions_before = container.of_type("transaction")

    reply = _confirm(approvals, card)

    assert reply["message"] == RESUME_MESSAGES[ResumeOutcome.NEEDS_NEW_CARD]
    assert reply["results"][0]["data"] == {"outcome": "needs_new_card"}
    assert container.of_type("inventory_item") == items_before
    assert container.of_type("transaction") == transactions_before
    assert container.of_type("change_event") == []
    stored = container.get(f"pending_approval:{card.approval_id}")
    assert (stored["status"], stored["cancel_reason"]) == ("cancelled", "replaced")
    # Saying it again: the household has it, so no card (rule 2 of 家裡多了東西).
    again, _ = said(reads, approvals, SHAMPOO)
    assert again.pending_approval is None


def test_the_item_a_line_creates_itself_does_not_stop_it():
    """Only other items count: reading back the line's own new item is not a duplicate."""
    container, reads, approvals = home()
    card = said(reads, approvals, SHAMPOO)[0].pending_approval
    (line,) = card.operations
    own = inventory(item_id=resolve_inventory_item_id(line.operation_id), name="洗髮精", unit="瓶")
    container.put(own)
    record = ApprovalRecord.from_document(container.get(f"pending_approval:{card.approval_id}"))
    record = record.model_copy(update={"approved_operation_ids": record.operation_ids})

    class _Planned:
        async def build(self, record):
            return "planned"

    router = CardWriteRouter(purchases=_Planned(), photo_items=_Planned(), household_items=reads)
    assert asyncio.run(router.build(record)) == "planned"


def test_no_location_said_leaves_it_empty_and_confirms():
    container, reads, approvals = home()
    card = said(
        reads, approvals, {"name": "雞蛋", "quantity": 6, "unit": "顆"}, message="鄰居送了 6 顆雞蛋"
    )[0].pending_approval

    line = card.operations[0]
    assert line.values["location"] is None
    assert line.description == f"會新增一個庫存品項：雞蛋 6 顆{SAID}"
    assert _confirm(approvals, card)["message"] == "已記錄：雞蛋 +6 顆（現在 6 顆）"
    assert items_named(container, "雞蛋")[0]["location"] is None


def test_cancelling_writes_nothing():
    container, reads, approvals = home()
    card = said(reads, approvals, SHAMPOO)[0].pending_approval

    with TestClient(resume_app(approvals)) as client:
        reply = client.post(
            "/agent/resume",
            json={"household_id": HOME, "approval_id": card.approval_id, "approved": False},
        ).json()

    assert reply["message"] == "已取消，沒有記錄任何東西。"
    assert items_named(container, "洗髮精") == []
    assert container.of_type("transaction") == []


def test_several_items_go_on_one_card_and_the_ticked_ones_are_written():
    container, reads, approvals = home()
    card = said(
        reads,
        approvals,
        SHAMPOO,
        {"name": "米", "quantity": 2, "unit": "包", "location": "廚房櫃子"},
        message="收到洗髮精一瓶和兩包米",
    )[0].pending_approval
    assert card.presentation is ApprovalPresentation.BATCH_ACTION

    reply = _confirm(approvals, card, [card.operations[1].operation_id])

    assert reply["message"] == "已記錄：米 +2 包（現在 2 包）"
    assert items_named(container, "洗髮精") == []
    assert len(items_named(container, "米")) == 1


@pytest.mark.parametrize(
    "household, name, have",
    [
        (inventory("shampoo", "洗髮精", 1, "瓶"), "洗髮精", "洗髮精"),  # same name
        (inventory("shampoo", "洗髮乳", 1, "瓶", aliases=["洗髮精"]), "洗髮精", "洗髮乳"),  # alias
        (inventory("shampoo", "洗髮精", 1, "瓶"), "ＳＨＡＭＰＯＯ", None),  # unrelated name
    ],
)
def test_an_item_the_household_has_gets_no_line(household, name, have):
    container, reads, approvals = home(household)

    response, model = said(reads, approvals, {**SHAMPOO, "name": name})

    if have is None:
        assert response.pending_approval is not None
        return
    assert response.pending_approval is None
    assert json.loads(tool_results(model)[-1]) == {
        "card": None,
        "not_on_card": [{"name": name, "reason": HAVE_IT.format(names=have)}],
    }
    assert container.of_type("pending_approval") == []


def test_the_same_item_said_twice_counts_once():
    container, reads, approvals = home()

    response, model = said(reads, approvals, SHAMPOO, {**SHAMPOO, "name": " 洗髮精 "})

    assert len(response.pending_approval.operations) == 1
    left_out = json.loads(tool_results(model)[-1])["not_on_card"]
    assert left_out == [{"name": "洗髮精", "reason": SAID_TWICE}]


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"name": "洗髮精", "quantity": 0, "unit": "瓶"}],
        [{"name": "洗髮精", "quantity": 1.5, "unit": "瓶"}],
        [{"name": "洗髮精", "quantity": True, "unit": "瓶"}],
        [{"name": " ", "quantity": 1, "unit": "瓶"}],
        [{"name": "洗髮精", "quantity": 1, "unit": ""}],
        [{"name": "洗髮精", "quantity": 1, "unit": "瓶", "location": 3}],
        ["洗髮精"],
    ],
)
def test_unusable_model_arguments_make_no_card(items):
    container, reads, approvals = home()

    response, model = said(reads, approvals, *items)

    assert response.pending_approval is None
    assert json.loads(tool_results(model)[-1]) == {"error": "no_items"}
    assert container.of_type("pending_approval") == []


def test_blank_location_counts_as_not_said():
    _, reads, approvals = home()

    card = said(reads, approvals, {**SHAMPOO, "location": "  "})[0].pending_approval

    assert card.operations[0].values["location"] is None


def test_one_card_per_message_and_none_while_an_open_card_is_shown():
    container, reads, approvals = home()
    the_agent = agent(
        reads,
        approvals,
        _call(ADD_STOCK, "call-1", items=[SHAMPOO]),
        _call(ADD_STOCK, "call-2", items=[{**SHAMPOO, "name": "米", "unit": "包"}]),
        AIMessage(content="請確認"),
    )
    first = asyncio.run(the_agent.respond(AgentRequest(household_id=HOME, message="收到洗髮精")))
    assert first.pending_approval is not None
    assert len(container.of_type("pending_approval")) == 1  # the second call made no card

    later = agent(
        reads,
        approvals,
        _call(ADD_STOCK, "call-1", items=[{**SHAMPOO, "name": "米", "unit": "包"}]),
        AIMessage(content="上一張卡還沒回答"),
    )
    response = asyncio.run(later.respond(AgentRequest(household_id=HOME, message="收到兩包米")))

    assert response.pending_approval.approval_id == first.pending_approval.approval_id
    assert json.loads(tool_results(later._model)[-1]) == {"error": "open_card_not_answered"}
    assert len(container.of_type("pending_approval")) == 1


def test_changing_the_quantity_keeps_the_location_and_wording():
    container, reads, approvals = home()
    old = said(reads, approvals, SHAMPOO)[0].pending_approval
    the_agent = agent(reads, approvals)  # no model call

    response = asyncio.run(
        the_agent.respond(
            AgentRequest(
                household_id=HOME,
                message="送出修改",
                replaces_approval_id=old.approval_id,
                card_changes=[CardChange(operation_id=old.operations[0].operation_id, quantity=3)],
            )
        )
    )

    assert response.message == CARD_CHANGED
    (line,) = response.pending_approval.operations
    assert line.values["location"] == "浴室"
    assert line.description == f"會新增一個庫存品項：洗髮精 3 瓶，放在浴室{SAID}"
    assert container.get(f"pending_approval:{old.approval_id}")["status"] == "cancelled"


def test_the_tool_is_offered_only_with_cards():
    _, reads, approvals = home()

    with_cards = agent(reads, approvals)._model.bound_tools
    without = agent(reads, approvals, stock=False)._model.bound_tools

    assert ADD_STOCK in {t["function"]["name"] for t in with_cards}
    assert ADD_STOCK not in {t["function"]["name"] for t in without}
    assert "add_stock" in load_system_prompt()


def test_without_the_tool_a_call_makes_no_card():
    container, reads, approvals = home()
    the_agent = agent(
        reads,
        approvals,
        _call(ADD_STOCK, "call-1", items=[SHAMPOO]),
        AIMessage(content="好"),
        stock=False,
    )

    response = asyncio.run(the_agent.respond(AgentRequest(household_id=HOME, message="收到")))

    assert response.pending_approval is None
    assert container.of_type("pending_approval") == []
