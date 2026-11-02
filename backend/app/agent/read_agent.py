"""A1 read-only Agent (Day 5): understand the question, look, answer.

- Two read tools only: inventory and shopping list. No write tool exists here,
  so no approval/pause is needed (AGENTS.md HITL rule 1).
- household_id always comes from the request, never from the model's arguments.
- Tools call the same services as the API routes (AGENTS.md architecture rule).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.core.dependencies import get_inventory_service, get_shopping_service
from app.models.agent import AgentRequest, AgentResponse, AgentResult, ResultKind
from app.tools.queries import QueryTools

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "agent_system.md"
MAX_STEPS = 8  # graph steps per message; stops endless tool loops

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_inventory",
            "description": "查詢家裡目前的庫存：品名、數量、單位、放置位置、別名。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_shopping",
            "description": "查詢待購清單：還要買的東西、數量、狀態。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class _State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    household_id: str
    inventory: list[dict[str, Any]]


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _text(message: AnyMessage) -> str:
    text = getattr(message, "text", message.content)
    return str(text) if isinstance(text, str) else text()


class ReadAgent:
    """Implements AgentRuntime.respond for read-only questions. resume is not offered."""

    def __init__(self, model: Any, tools: QueryTools, system_prompt: str) -> None:
        self._model = model.bind_tools(TOOL_SCHEMAS)
        self._handlers: dict[str, Callable[[str], Awaitable[list[Any]]]] = {
            "list_inventory": tools.list_inventory,
            "list_shopping": tools.list_shopping,
        }
        self._system_prompt = system_prompt
        self._graph = self._build_graph()

    def _build_graph(self):
        async def think(state: _State) -> _State:
            reply = await self._model.ainvoke(state["messages"])
            return {"messages": [reply]}

        async def look(state: _State) -> _State:
            update: _State = {"messages": []}
            for call in state["messages"][-1].tool_calls:
                handler = self._handlers.get(call["name"])
                if handler is None:
                    content = f"沒有這個工具：{call['name']}"
                else:
                    items = await handler(state["household_id"])
                    rows = [item.model_dump(mode="json") for item in items]
                    if call["name"] == "list_inventory":
                        update["inventory"] = rows
                    content = json.dumps(rows, ensure_ascii=False)
                update["messages"].append(ToolMessage(content, tool_call_id=call["id"]))
            return update

        def next_step(state: _State) -> str:
            return "look" if state["messages"][-1].tool_calls else END

        builder = StateGraph(_State)
        builder.add_node("think", think)
        builder.add_node("look", look)
        builder.add_edge(START, "think")
        builder.add_conditional_edges("think", next_step, ["look", END])
        builder.add_edge("look", "think")
        return builder.compile()

    async def respond(self, request: AgentRequest) -> AgentResponse:
        start: _State = {
            "household_id": request.household_id,
            "messages": [
                SystemMessage(self._system_prompt),
                HumanMessage(request.message),
            ],
        }
        try:
            final = await self._graph.ainvoke(start, {"recursion_limit": MAX_STEPS})
        except GraphRecursionError:
            return AgentResponse(message="這個問題我查了太多次還是答不出來，請換個方式問問看。")

        results = []
        if "inventory" in final:
            results.append(
                AgentResult(
                    kind=ResultKind.INVENTORY,
                    summary="庫存查詢結果",
                    data={"items": final["inventory"]},
                )
            )
        return AgentResponse(message=_text(final["messages"][-1]), results=results)


@lru_cache(maxsize=1)
def get_read_agent() -> ReadAgent | None:
    """FastAPI dependency. None when Azure OpenAI is not configured (route answers 501)."""
    from app.agent.llm import build_chat_model, load_azure_settings

    settings = load_azure_settings()
    if settings is None:
        return None
    tools = QueryTools(get_inventory_service(), get_shopping_service())
    return ReadAgent(build_chat_model(settings), tools, load_system_prompt())
