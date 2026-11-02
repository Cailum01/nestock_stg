"""A1 read-only Agent tests (Day 5). A scripted fake model; no network, no Azure cost."""

import asyncio
import json

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.read_agent import ReadAgent, get_read_agent, load_system_prompt
from app.core.dependencies import get_inventory_service, get_shopping_service
from app.main import create_app
from app.models.agent import AgentRequest, ResultKind
from app.tools.queries import QueryTools

HOUSEHOLD = "demo-household"


class ScriptedModel:
    """Stands in for Azure OpenAI: returns prepared replies and records what it saw."""

    def __init__(self, *replies: AIMessage) -> None:
        self.replies = list(replies)
        self.seen: list[list] = []
        self.bound_tools: list = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def _call(name: str, call_id: str = "call-1", **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _agent(model: ScriptedModel) -> ReadAgent:
    tools = QueryTools(get_inventory_service(), get_shopping_service())
    return ReadAgent(model, tools, load_system_prompt())


def _ask(agent: ReadAgent, message: str, household_id: str = HOUSEHOLD):
    request = AgentRequest(household_id=household_id, message=message)
    return asyncio.run(agent.respond(request))


def _tool_rows(model: ScriptedModel) -> list:
    tool_messages = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    return json.loads(tool_messages[-1].content)


def test_only_read_tools_are_offered():
    model = ScriptedModel(AIMessage(content="你好"))
    _agent(model)
    names = {tool["function"]["name"] for tool in model.bound_tools}
    assert names == {"list_inventory", "list_shopping"}


def test_inventory_question_looks_then_answers():
    model = ScriptedModel(_call("list_inventory"), AIMessage(content="還有 2 包白米，在廚房櫃子。"))
    response = _ask(_agent(model), "家裡還有白米嗎？")

    assert response.message == "還有 2 包白米，在廚房櫃子。"
    assert response.pending_approval is None
    rows = _tool_rows(model)  # the model's second turn saw the real service data
    assert [row["name"] for row in rows] == ["白米"]
    assert response.results[0].kind == ResultKind.INVENTORY
    assert response.results[0].data["items"][0]["location"] == "廚房櫃子"


def test_shopping_question_uses_shopping_list():
    model = ScriptedModel(_call("list_shopping"), AIMessage(content="還要買 1 瓶牛奶。"))
    response = _ask(_agent(model), "我還要買什麼？")

    assert [row["name"] for row in _tool_rows(model)] == ["牛奶"]
    assert response.results == []  # inventory card only when inventory was looked up


def test_greeting_needs_no_tool():
    model = ScriptedModel(AIMessage(content="你好！想查什麼？"))
    response = _ask(_agent(model), "你好")

    assert response.message == "你好！想查什麼？"
    assert len(model.seen) == 1
    assert response.results == []


def test_household_comes_from_request_not_model():
    # Even if the model tries to pass another household, the request's scope is used.
    model = ScriptedModel(
        _call("list_inventory", household_id="demo-household"),
        AIMessage(content="庫存裡沒有找到。"),
    )
    _ask(_agent(model), "家裡有什麼？", household_id="another-household")
    assert _tool_rows(model) == []


def test_unknown_tool_is_refused():
    model = ScriptedModel(_call("add_inventory"), AIMessage(content="目前只能幫你查詢。"))
    response = _ask(_agent(model), "把鉛筆存進庫存")

    tool_message = [m for m in model.seen[-1] if isinstance(m, ToolMessage)][-1]
    assert "沒有這個工具" in tool_message.content
    assert response.message == "目前只能幫你查詢。"


def test_endless_tool_loop_is_stopped():
    model = ScriptedModel(*[_call("list_inventory", call_id=f"c{i}") for i in range(20)])
    response = _ask(_agent(model), "家裡有什麼？")
    assert "換個方式" in response.message


def test_route_answers_and_writes_nothing():
    model = ScriptedModel(_call("list_inventory"), AIMessage(content="還有 2 包白米。"))
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: _agent(model)
    with TestClient(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]
        response = client.post(
            "/agent/messages", json={"household_id": HOUSEHOLD, "message": "還有白米嗎"}
        )
        assert response.status_code == 200
        assert response.json()["message"] == "還有 2 包白米。"
        assert before == [client.get(path).json() for path in ["/inventory", "/shopping"]]
