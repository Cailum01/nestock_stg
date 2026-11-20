"""A1: a card made in a message always reaches the App (PR #216, Issue #213 point 2).

Before: after a photo card the model could keep calling tools until `MAX_STEPS`; the
reply was then only 「查了太多次」, with no card, while the stored card blocked new cards
until it expired. Now (1) after a photo card the model speaks once more with no tools,
and the message ends; (2) a message stopped at `MAX_STEPS` still returns a card it
stored, with fixed text. With no card the stop reply is unchanged.
"""

import asyncio
import json

from langchain_core.messages import AIMessage, ToolMessage
from test_photo_cards import _agent as photo_agent
from test_purchase_cards import HOME, ScriptedModel, agent, ask, purchase_reply, setup
from test_read_agent import _agent as read_agent
from test_read_agent import _ask, _ask_photo, _call, _detected, _Inventory

from app.agent.read_agent import CARD_MADE_STOPPED, OPEN_CARD_REMINDER, TOO_MANY_STEPS
from app.models.agent import AgentRequest, ResultKind

MOUSE = _detected("滑鼠", unit="個")
TISSUE = {"shopping_item_id": "tissue-1", "quantity": 2}


class _SplitModel:
    """`bind_tools` returns a separate model, so a test sees which calls had tools."""

    def __init__(self, with_tools, without_tools) -> None:
        self.with_tools = ScriptedModel(*with_tools)
        self.without_tools = ScriptedModel(*without_tools)

    def bind_tools(self, tools):
        return self.with_tools.bind_tools(tools)

    async def ainvoke(self, messages):
        return await self.without_tools.ainvoke(messages)


def _looking(count: int, name: str = "list_inventory") -> list[AIMessage]:
    """`count` more tool calls: a model that does not stop looking."""
    return [_call(name, f"call-loop-{n}") for n in range(count)]


def _pending(container) -> list[str]:
    return [d["status"] for d in container.of_type("pending_approval")]


# ----- (1) after a photo card the model only speaks ----------------------------------


def test_after_a_photo_card_the_model_speaks_once_with_no_tools():
    model = _SplitModel(
        # The card is made by the first look; the extra calls are never asked for.
        [_call("analyze_photo"), *_looking(3, "add_stock")],
        [AIMessage(content="看到一個滑鼠，幫你做好新增的確認卡了，請確認或取消。")],
    )
    agent, container = photo_agent(model, _Inventory(), MOUSE)

    response = _ask_photo(agent)

    assert response.message == "看到一個滑鼠，幫你做好新增的確認卡了，請確認或取消。"
    [line] = response.pending_approval.operations
    assert line.action == "create_item"
    assert _pending(container) == ["pending"]
    assert [result.kind for result in response.results] == [ResultKind.VISION]
    assert len(model.with_tools.seen) == 1  # only the first look
    assert len(model.without_tools.seen) == 1  # then it speaks, once
    # It speaks knowing the card (and its photo note), so it can tell the user.
    told = [m for m in model.without_tools.seen[0] if isinstance(m, ToolMessage)]
    assert json.loads(told[-1].content)["new_item_card"]["lines"] == [line.description]


def test_a_photo_with_no_card_goes_on_as_before():
    model = _SplitModel(
        [
            _call("analyze_photo"),
            _call("list_inventory", "call-2"),
            AIMessage(content="沒有滑鼠。"),
        ],
        [],
    )
    agent, container = photo_agent(model, _Inventory(), MOUSE, photo_cards=False)

    response = _ask_photo(agent)

    assert response.message == "沒有滑鼠。"
    assert response.pending_approval is None
    assert container.of_type("pending_approval") == []
    assert len(model.with_tools.seen) == 3
    assert model.without_tools.seen == []


# ----- (2) a stop at MAX_STEPS keeps a stored card ------------------------------------


def test_a_stop_after_a_photo_card_still_returns_the_card():
    # Three looks first, so the photo card is made on the last step there is.
    model = ScriptedModel(
        *_looking(3), _call("analyze_photo", "call-photo"), AIMessage(content="不會用到")
    )
    agent, container = photo_agent(model, _Inventory(), MOUSE)

    response = _ask_photo(agent)

    assert response.message == CARD_MADE_STOPPED
    [line] = response.pending_approval.operations
    assert line.action == "create_item"
    assert _pending(container) == ["pending"]
    # What it found before the stop is shown too, as in any reply.
    assert [result.kind for result in response.results] == [
        ResultKind.INVENTORY,
        ResultKind.VISION,
    ]
    assert [m.content for m in model.replies] == ["不會用到"]


def test_a_stop_after_a_spoken_card_still_returns_the_card():
    container, reader, approvals = setup()
    model = ScriptedModel(
        purchase_reply(TISSUE, call_id="call-1"),
        *_looking(3, "list_shopping"),
        AIMessage(content="不會用到"),
    )

    response = ask(agent(model, reader, approvals))

    assert response.message == CARD_MADE_STOPPED
    [line] = response.pending_approval.operations
    assert line.action == "complete_purchase"
    assert _pending(container) == ["pending"]


def test_a_stop_with_no_card_says_too_many_steps_as_before():
    model = ScriptedModel(*_looking(4), AIMessage(content="不會用到"))

    response = _ask(read_agent(model), "家裡還有白米嗎？")

    assert response.message == TOO_MANY_STEPS
    assert response.pending_approval is None
    assert response.results == []


def test_a_stop_with_the_open_card_shown_again_gives_the_reminder():
    container, reader, approvals = setup()
    first = ask(
        agent(
            ScriptedModel(purchase_reply(TISSUE, call_id="call-1"), AIMessage(content="請確認")),
            reader,
            approvals,
        )
    ).pending_approval
    model = ScriptedModel(*_looking(4, "list_shopping"), AIMessage(content="不會用到"))

    response = ask(agent(model, reader, approvals), "家裡還有衛生紙嗎？")

    assert response.message == OPEN_CARD_REMINDER
    assert response.pending_approval.approval_id == first.approval_id
    assert response.results[-1].kind is ResultKind.ACTION_RESULT
    assert response.results[-1].summary == OPEN_CARD_REMINDER
    assert _pending(container) == ["pending"]  # no new card


def test_a_stop_while_changing_a_card_retires_the_old_card_first():
    container, reader, approvals = setup()
    old = ask(
        agent(
            ScriptedModel(purchase_reply(TISSUE, call_id="call-1"), AIMessage(content="請確認")),
            reader,
            approvals,
        )
    ).pending_approval
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        *_looking(3, "list_shopping"),
        AIMessage(content="不會用到"),
    )
    request = AgentRequest(
        household_id=HOME, message="改成 1 包", replaces_approval_id=old.approval_id
    )

    response = asyncio.run(agent(model, reader, approvals).respond(request))

    assert response.message == CARD_MADE_STOPPED
    new = response.pending_approval
    assert new.approval_id != old.approval_id
    statuses = {d["approval_id"]: d["status"] for d in container.of_type("pending_approval")}
    assert statuses == {old.approval_id: "cancelled", new.approval_id: "pending"}
