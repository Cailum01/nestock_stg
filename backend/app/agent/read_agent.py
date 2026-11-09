"""A1 read-only Agent (Day 5, photo reading Day 8): understand, look, answer.

- Read tools only: inventory, shopping list and photo recognition. No write tool
  exists here, so no approval/pause is needed (AGENTS.md HITL rule 1).
- household_id and image_ref always come from the request, never from the
  model's arguments.
- A photo is used only after the media is verified to belong to the request's
  household (open-decisions D-5). An unknown or foreign photo is refused before
  the model is called.
- scene_type is passed only when the user hinted it; otherwise A2 decides.
- A2 reports only what the photo shows. A1 decides which household item it is
  (Issue #44): exact name/alias here, and the model compares an unmatched name
  with the household item list in the same call (Issue #36 layers 2 and 3).
- Recognition results are shown only. Nothing is written to inventory here.
- Tools call the same services as the API routes (AGENTS.md architecture rule).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.core.dependencies import (
    get_inventory_service,
    get_media_storage_service,
    get_shopping_service,
    get_vision_service,
)
from app.models.agent import AgentRequest, AgentResponse, AgentResult, ResultKind
from app.models.domain import InventoryItem
from app.models.vision import SceneType, VisionAnalysisResult
from app.services.item_matching import ItemMatch, ItemMatcher, MatchMethod, match_term
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.vision import VisionService
from app.tools.queries import QueryTools

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "agent_system.md"
MAX_STEPS = 8  # graph steps per message; stops endless tool loops
PHOTO_NOT_SUPPORTED = (
    "使用者這次附了一張照片，但你目前看不到照片內容（照片辨識還沒接上）。"
    "請先清楚告訴使用者：照片辨識還沒開放，目前只能用文字查詢；"
    "再用文字能回答的部分回答。不要假裝看過照片，也不要猜測照片裡有什麼。"
)
PHOTO_ATTACHED = "使用者這次附了一張照片。請先呼叫 analyze_photo 看照片內容，再回答。"
PHOTO_NOT_FOUND = "找不到這張照片，請重新拍一次或重新上傳。"
LOW_CONFIDENCE = 0.6  # below this, the Agent says it is unsure (Issue #17)
NO_PHOTO = "這次沒有附照片，無法辨識。"
PHOTO_FAILED = json.dumps({"error": "photo_analysis_failed"})
"""Tool result when A2's recognition raises (timeout, upstream error): the Agent still
answers and asks for another photo instead of the whole request failing (502)."""

logger = logging.getLogger(__name__)

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
    {
        "type": "function",
        "function": {
            "name": "analyze_photo",
            "description": (
                "辨識使用者這次附上的照片：認出哪些東西、數量、單位，並和庫存比對。"
                "只有使用者附了照片時才呼叫。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_type": {
                        "type": "string",
                        "enum": [scene.value for scene in SceneType],
                        "description": (
                            "照片類型。只有使用者有提示時才填："
                            "說是發票或收據填 receipt，說是櫃子或冰箱填 pantry_scene，"
                            "說是一樣東西填 single_item。沒有提示就不要填。"
                        ),
                    }
                },
            },
        },
    },
]


class _State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    household_id: str
    image_ref: str
    inventory: list[dict[str, Any]]
    vision: dict[str, Any]


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _scene(value: Any) -> SceneType | None:
    """The model's optional hint; anything unknown means "let A2 decide"."""
    try:
        return SceneType(value) if value else None
    except ValueError:
        return None


def _match_view(match: ItemMatch) -> dict[str, Any]:
    """A1's answer to "which household item is this?" for one detected item."""
    view: dict[str, Any] = {"method": match.method.value}
    item = match.matched
    if item is not None:
        view.update(item_id=item.item_id, item_name=item.name, item_unit=item.unit)
    elif match.ambiguous:
        view["choices"] = [
            {"item_id": choice.item_id, "item_name": choice.name} for choice in match.items
        ]
    return view


def _household_item_view(item: InventoryItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "name": item.name,
        "aliases": list(item.aliases or []),
        "unit": item.unit,
    }


def _vision_view(
    result: VisionAnalysisResult,
    matches: list[ItemMatch],
    household_items: list[InventoryItem],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(view for the model, data for the App).

    Uses only what A2 observed plus A1's own matching; A2's matched_inventory_id,
    candidate_matches and match_status are ignored so A2 can remove them (Issue #44).
    """
    items = []
    for detected, match in zip(result.detected_items, matches, strict=True):
        items.append(
            {
                "name": detected.canonical_name,
                "seen_as": detected.raw_detected_name,
                "quantity": detected.quantity,
                "unit": detected.unit if detected.unit_confirmed else None,
                "unit_confirmed": detected.unit_confirmed,
                "confidence": detected.confidence,
                "low_confidence": detected.confidence < LOW_CONFIDENCE,
                "expiry_date": detected.expiry_date.isoformat() if detected.expiry_date else None,
                "match": _match_view(match),
            }
        )
    data: dict[str, Any] = {
        "image_ref": result.image_ref,
        "scene_type": result.scene_type.value,
        "items": items,
    }
    model_view: dict[str, Any] = {"scene_type": result.scene_type.value, "items": items}
    if any(match.method == MatchMethod.NONE for match in matches):
        # Layer 3: the model compares unmatched names with the household's items.
        model_view["household_items"] = [_household_item_view(i) for i in household_items]
    return model_view, data


def _text(message: AnyMessage) -> str:
    text = getattr(message, "text", message.content)
    return str(text) if isinstance(text, str) else text()


class ReadAgent:
    """Implements AgentRuntime.respond for read-only questions. resume is not offered."""

    def __init__(
        self,
        model: Any,
        tools: QueryTools,
        system_prompt: str,
        vision: VisionService | None = None,
        media: MediaStorageService | None = None,
        matcher: ItemMatcher | None = None,
    ) -> None:
        self._model = model.bind_tools(TOOL_SCHEMAS)
        self._handlers: dict[str, Callable[[str], Awaitable[list[Any]]]] = {
            "list_inventory": tools.list_inventory,
            "list_shopping": tools.list_shopping,
        }
        self._system_prompt = system_prompt
        self._vision = vision
        self._media = media
        self._matcher = matcher or ItemMatcher(tools.inventory, tools.shopping)
        self._graph = self._build_graph()

    @property
    def reads_photos(self) -> bool:
        return self._vision is not None and self._media is not None

    def _build_graph(self):
        async def think(state: _State) -> _State:
            reply = await self._model.ainvoke(state["messages"])
            return {"messages": [reply]}

        async def look(state: _State) -> _State:
            update: _State = {"messages": []}
            for call in state["messages"][-1].tool_calls:
                handler = self._handlers.get(call["name"])
                if call["name"] == "analyze_photo":
                    content, vision = await self._analyze(state, call.get("args") or {})
                    if vision is not None:
                        update["vision"] = vision
                elif handler is None:
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

    async def _analyze(
        self, state: _State, args: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """Run A2's recognition on the request's own (already verified) photo."""
        image_ref = state.get("image_ref")
        if not image_ref or self._vision is None:
            return NO_PHOTO, None
        household_id = state["household_id"]
        try:
            result = await self._vision.analyze_image(
                household_id, image_ref, _scene(args.get("scene_type"))
            )
        except Exception:  # A2's recognition failed; nothing was read or written
            logger.warning("Photo analysis failed for image_ref=%s", image_ref, exc_info=True)
            return PHOTO_FAILED, None
        household_items = await self._matcher.household_items(household_id)
        matches = [
            match_term(detected.canonical_name, household_items)
            for detected in result.detected_items
        ]
        model_view, data = _vision_view(result, matches, household_items)
        return json.dumps(model_view, ensure_ascii=False), data

    async def respond(self, request: AgentRequest) -> AgentResponse:
        messages: list[AnyMessage] = [SystemMessage(self._system_prompt)]
        start: _State = {"household_id": request.household_id}
        if request.image_ref and self.reads_photos:
            # D-5: the photo must belong to this household before anything reads it.
            if not await self._media.verify_media_exists(request.household_id, request.image_ref):
                return AgentResponse(message=PHOTO_NOT_FOUND)
            start["image_ref"] = request.image_ref
            messages.append(SystemMessage(PHOTO_ATTACHED))
        elif request.image_ref:  # accepted, not read: no vision service configured
            messages.append(SystemMessage(PHOTO_NOT_SUPPORTED))
        messages.append(HumanMessage(request.message))
        start["messages"] = messages
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
        if "vision" in final:
            results.append(
                AgentResult(
                    kind=ResultKind.VISION,
                    summary="照片辨識結果（尚未寫入庫存）",
                    data=final["vision"],
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

    inventory = get_inventory_service()
    tools = QueryTools(inventory, get_shopping_service())
    return ReadAgent(
        build_chat_model(settings),
        tools,
        load_system_prompt(),
        vision=get_vision_service(),
        media=get_media_storage_service(),
    )
