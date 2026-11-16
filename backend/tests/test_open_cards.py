"""A1: an open card comes back with the next message (contract section 8, Issue #91).

After 清除對話 or reopening the App, a card is gone from the screen but still open in
the backend. The next reply shows the newest open card again, with the same
`approval_id`, and the message makes no new card. Same faked Cosmos container and
real services as `test_purchase_cards`.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_purchase_cards import (
    HOME,
    T0,
    FakeCosmosContainer,
    ScriptedModel,
    agent,
    ask,
    inventory,
    purchase_reply,
    setup,
    shopping,
    tool_results,
)

from app.agent.cards import OPEN_CARD_FIRST, PURCHASE_TOOL_SCHEMA, PurchaseCards
from app.agent.llm import build_chat_model, load_azure_settings
from app.agent.read_agent import OPEN_CARD, TOOL_SCHEMAS, load_system_prompt
from app.models.agent import ResumeRequest
from app.services.approvals import ApprovalService
from app.services.cosmos_read import CosmosReadRepository
from app.services.cosmos_write import CosmosWriteRepository
from app.services.purchase_inputs import CardInputResolver
from app.services.shopping.workflow import ShoppingWriteBuilder


def run(coro):
    return asyncio.run(coro)


def clocked_setup():
    """Two list entries the household has, and a clock the test can move."""
    container = FakeCosmosContainer()
    for record in (
        inventory(),
        shopping(),
        inventory(item_id="detergent", name="洗衣精", unit="瓶"),
        shopping(shopping_item_id="detergent-1", name="洗衣精", item_id="detergent", unit="瓶"),
    ):
        container.put(record)
    now = [T0]
    reader = CosmosReadRepository(container)
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        ShoppingWriteBuilder(container, CardInputResolver(reader)),
        clock=lambda: now[0],
    )
    return container, reader, approvals, now


def make_card(reader, approvals, shopping_item_id="tissue-1", quantity=2):
    _, card = run(
        PurchaseCards(reader, approvals).propose(
            HOME, {"items": [{"shopping_item_id": shopping_item_id, "quantity": quantity}]}
        )
    )
    return card


def next_message(reader, approvals, message="家裡還有洗衣精嗎？", *replies):
    model = ScriptedModel(*(replies or (AIMessage(content="還有 1 瓶。"),)))
    return ask(agent(model, reader, approvals), message), model


def shown_to_model(model):
    return [m.content for m in model.seen[0] if isinstance(m, SystemMessage)][-1]


def stored_cards(container):
    return {doc["approval_id"]: doc["status"] for doc in container.of_type("pending_approval")}


def test_an_open_card_comes_back_with_the_next_message():
    container, reader, approvals = setup()
    old = make_card(reader, approvals)

    response, model = next_message(reader, approvals)

    assert response.pending_approval.model_dump(mode="json") == old
    shown = shown_to_model(model)
    assert "沒確認" in shown and old["operations"][0]["description"] in shown
    assert "transaction_id" not in shown
    assert "總共還有" not in shown
    assert stored_cards(container) == {old["approval_id"]: "pending"}  # nothing new stored
    assert container.get("inventory_item:tissue")["quantity"] == 1  # nothing written


def test_no_new_card_is_made_while_an_open_card_is_shown():
    container, reader, approvals = setup()
    old = make_card(reader, approvals)

    response, model = next_message(
        reader,
        approvals,
        "衛生紙買回來了，2 包",
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 2}, call_id="call-1"),
        AIMessage(content="上面那張卡還沒確認，請先確認或取消。"),
    )

    assert tool_results(model) == [OPEN_CARD_FIRST]
    assert response.pending_approval.approval_id == old["approval_id"]
    assert stored_cards(container) == {old["approval_id"]: "pending"}


def test_the_newest_of_several_open_cards_comes_back_with_the_count():
    _, reader, approvals, now = clocked_setup()
    older = make_card(reader, approvals, "tissue-1")
    now[0] = T0 + timedelta(minutes=5)
    newer = make_card(reader, approvals, "detergent-1")
    now[0] = T0 + timedelta(minutes=6)

    response, model = next_message(reader, approvals, "嗨")

    assert response.pending_approval.approval_id == newer["approval_id"]
    assert "總共還有 2 張" in shown_to_model(model)

    run(
        approvals.resume(
            ResumeRequest(household_id=HOME, approval_id=newer["approval_id"], approved=False)
        )
    )
    response, model = next_message(reader, approvals, "嗨")

    assert response.pending_approval.approval_id == older["approval_id"]
    assert "總共還有" not in shown_to_model(model)


def test_answered_and_expired_cards_do_not_come_back():
    container, reader, approvals, now = clocked_setup()
    confirmed = make_card(reader, approvals, "tissue-1")
    run(
        approvals.resume(
            ResumeRequest(household_id=HOME, approval_id=confirmed["approval_id"], approved=True)
        )
    )
    expired = make_card(reader, approvals, "detergent-1")
    now[0] = T0 + timedelta(minutes=31)

    response, model = next_message(reader, approvals, "嗨")

    assert response.pending_approval is None
    assert all("沒確認" not in m.content for m in model.seen[0] if isinstance(m, SystemMessage))
    assert stored_cards(container) == {
        confirmed["approval_id"]: "completed",
        expired["approval_id"]: "expired",  # recorded by the same lookup
    }


def test_if_open_cards_cannot_be_listed_the_message_is_still_answered(monkeypatch):
    _, reader, approvals = setup()
    make_card(reader, approvals)

    async def unreachable(**_):
        raise ConnectionError("Cosmos is unreachable")

    monkeypatch.setattr(approvals, "list_open", unreachable)
    response, model = next_message(reader, approvals)

    assert response.message == "還有 1 瓶。"
    assert response.pending_approval is None
    assert all("沒確認" not in m.content for m in model.seen[0] if isinstance(m, SystemMessage))


# ----- live model (run with -m live_eval, Issue #127) ----------------------------------

OPEN_TISSUE_CARD = OPEN_CARD.format(
    card=json.dumps(
        [
            {
                "action": "complete_purchase",
                "description": "衛生紙 +2 包（從待購清單完成購買）",
                "values": {"shopping_item_id": "tissue-1", "quantity": 2, "unit": "包"},
            }
        ],
        ensure_ascii=False,
    ),
    more="",
)
LIVE_ROWS = {
    "list_shopping": [
        {
            "shopping_item_id": "tissue-1",
            "name": "衛生紙",
            "quantity": 2,
            "unit": "包",
            "status": "pending",
            "item_id": "tissue",
        }
    ],
    "list_inventory": [
        {
            "item_id": "detergent",
            "name": "洗衣精",
            "quantity": 1,
            "unit": "瓶",
            "location": "陽台洗衣機旁",
        }
    ],
}


@pytest.fixture(scope="module")
def live_model():
    settings = load_azure_settings()
    if settings is None:
        pytest.skip("Azure OpenAI is not configured; live check not run")
    return build_chat_model(settings).bind_tools([*TOOL_SCHEMAS, PURCHASE_TOOL_SCHEMA])


@pytest.mark.live_eval
@pytest.mark.parametrize(
    "utterance, expect",
    [("家裡還有洗衣精嗎？", {"list_inventory"}), ("衛生紙買回來了，2 包", set())],
    ids=["answers-the-question", "no-second-card"],
)
def test_live_model_answers_reminds_and_makes_no_card(live_model, utterance, expect):
    """With an open card shown again, the model answers, reminds, and makes no card."""
    messages = [
        SystemMessage(load_system_prompt()),
        SystemMessage(OPEN_TISSUE_CARD),
        HumanMessage(utterance),
    ]
    called: set[str] = set()
    for _ in range(3):
        reply = live_model.invoke(messages)
        if not reply.tool_calls:
            break
        called |= {call["name"] for call in reply.tool_calls}
        messages.append(reply)
        for call in reply.tool_calls:
            rows = LIVE_ROWS.get(call["name"], [])
            content = json.dumps(rows, ensure_ascii=False)
            messages.append(ToolMessage(content, tool_call_id=call["id"]))

    assert expect <= called, (utterance, called)
    assert "complete_purchase" not in called, (utterance, called)
    assert not reply.tool_calls, (utterance, "still calling tools")
    assert "確認" in reply.content, (utterance, reply.content)
