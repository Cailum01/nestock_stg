"""A1 read-only Agent (Day 5, photo reading Day 8): understand, look, answer.

- Read tools only: inventory, shopping list, photo recognition and price search.
  No write tool exists here, so no approval/pause is needed (AGENTS.md HITL rule 1).
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
- Price search uses A2's provider (v8 section 6): options are shown only, prices are
  never stored, and the program, not the model, marks the lowest price found.
- Tools call the same services as the API routes (AGENTS.md architecture rule).
- Forecasts come from B2's ForecastService (the same service as `/forecast`). They are
  estimates for "what is running low" and "how long will X last"; the program, not
  the model, picks the items to restock and rounds the days. Nothing is written.
"""

from __future__ import annotations

import datetime
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Protocol, TypedDict

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
from app.models.commerce import PurchaseOption
from app.models.domain import InventoryItem
from app.models.vision import SceneType, VisionAnalysisResult
from app.services.commerce.biggo import BigGoCommerceProvider
from app.services.item_matching import ItemMatch, ItemMatcher, MatchMethod, match_term
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.vision import VisionService
from app.tools.queries import QueryTools

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "agent_system.md"
MAX_STEPS = 8  # graph steps per message; stops endless tool loops
MAX_PURCHASE_OPTIONS = 5
PRICE_NOT_SUPPORTED = json.dumps({"error": "price_search_not_available"})
PRICE_FAILED = json.dumps({"error": "price_search_failed"})
"""Tool result when A2's price search raises: the Agent still answers (no 502)."""
NO_ITEM_NAME = "沒有指定要查價的品名。"
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

FORECAST_TOOLS = {"get_reorder_candidates", "get_consumption_forecast"}
FORECAST_NOT_AVAILABLE = json.dumps({"error": "forecast_not_available"})
FORECAST_FAILED = json.dumps({"error": "forecast_failed"})
"""Tool result when B2's forecast raises: the Agent still answers (no 502)."""
NO_FORECAST_ITEM = "沒有指定要預估的品名。"


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


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
            "name": "get_reorder_candidates",
            "description": (
                "查家裡哪些東西快用完、建議補貨（預估，不會改庫存）。"
                "使用者問什麼快用完了、要補什麼時才呼叫。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_consumption_forecast",
            "description": (
                "查某樣東西預估還能用幾天（預估，不會改庫存）。"
                "使用者明確問某樣東西還能用多久、什麼時候用完時才呼叫。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {
                        "type": "string",
                        "description": "要預估的品名，例如「衛生紙」。",
                    }
                },
                "required": ["item_name"],
            },
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
    {
        "type": "function",
        "function": {
            "name": "search_purchase_options",
            "description": (
                "查某樣東西目前找得到的購買選項和價格（比價）。"
                "使用者問哪裡買、多少錢、哪裡比較便宜時才呼叫。只查詢，不會下單或加入待購。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {
                        "type": "string",
                        "description": "要查價的品名，例如「衛生紙」。",
                    }
                },
                "required": ["item_name"],
            },
        },
    },
]


class PurchaseOptionSearch(Protocol):
    """A2's price search (`BigGoCommerceProvider.search_options`)."""

    async def search_options(self, keyword: str, limit: int = 5) -> list[PurchaseOption]: ...


class _State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    household_id: str
    image_ref: str
    forecasts: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    vision: dict[str, Any]
    purchase_options: list[dict[str, Any]]


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


def _forecast_row(item: InventoryItem, forecast: Any) -> dict[str, Any]:
    """One item's forecast for the model and the App. Days are rounded by the program."""
    days = forecast.days_until_empty
    return {
        "item_id": item.item_id,
        "name": item.name,
        "quantity": item.quantity,
        "unit": item.unit,
        "status": forecast.status.value,
        "days_left": round(days) if days is not None else None,
        "consider_restock": forecast.suggested_action.value == "consider_restock",
    }


def _reorder_view(
    items: list[InventoryItem], summary: Any
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """B2's `reorder_candidates` result for the model and the App.

    B2 picks the items and their order; A1 keeps that order and only rounds the days.
    Items no longer in the household list (read again here) are left out.
    """
    by_id = {item.item_id: item for item in items}
    rows = [_forecast_row(by_id[f.item_id], f) for f in summary.candidates if f.item_id in by_id]
    view = {
        "running_low": rows,
        "estimated_count": summary.estimated_count,
        "insufficient_data_count": summary.insufficient_data_count,
    }
    card = {"kind": "reorder_candidates", **view} if rows else None
    return view, card


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


def _by_price(options: list[PurchaseOption]) -> list[PurchaseOption]:
    """Cheapest first; options without a price go last in their original order."""
    priced = sorted((o for o in options if o.estimated_price is not None), key=_price)
    return priced + [o for o in options if o.estimated_price is None]


def _price(option: PurchaseOption) -> float:
    return option.estimated_price if option.estimated_price is not None else 0.0


def _price_view(
    item_name: str, options: list[PurchaseOption]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(view for the model, data for the App). Nothing here is stored."""
    ordered = _by_price(options)
    lowest = next((o.option_id for o in ordered if o.estimated_price is not None), None)
    model_view = {
        "item_name": item_name,
        "options": [
            {
                "title": option.title,
                "store": option.store,
                "estimated_price": option.estimated_price,
                "unit_spec": option.unit_spec,
                "source": option.source,
                "lowest_price_found": option.option_id == lowest,
            }
            for option in ordered
        ],
    }
    data = {
        "item_name": item_name,
        "demo": any(option.source == "demo" for option in ordered),
        "options": [option.model_dump(mode="json") for option in ordered],
    }
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
        forecast: Any | None = None,
        clock: Callable[[], datetime.datetime] = _utc_now,
        vision: VisionService | None = None,
        media: MediaStorageService | None = None,
        matcher: ItemMatcher | None = None,
        commerce: PurchaseOptionSearch | None = None,
    ) -> None:
        self._model = model.bind_tools(TOOL_SCHEMAS)
        self._handlers: dict[str, Callable[[str], Awaitable[list[Any]]]] = {
            "list_inventory": tools.list_inventory,
            "list_shopping": tools.list_shopping,
        }
        self._system_prompt = system_prompt
        # B2's ForecastService: reorder_candidates / forecast_household(household_id, now)
        self._forecast = forecast
        self._clock = clock
        self._vision = vision
        self._media = media
        self._commerce = commerce
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
                if call["name"] in FORECAST_TOOLS:
                    content, card = await self._forecast_answer(state, call)
                    if card is not None:
                        earlier = update.get("forecasts", state.get("forecasts", []))
                        update["forecasts"] = [*earlier, card]
                elif call["name"] == "analyze_photo":
                    content, vision = await self._analyze(state, call.get("args") or {})
                    if vision is not None:
                        update["vision"] = vision
                elif call["name"] == "search_purchase_options":
                    content, found = await self._search_prices(call.get("args") or {})
                    if found is not None:
                        earlier = update.get("purchase_options", state.get("purchase_options", []))
                        update["purchase_options"] = [*earlier, found]
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

    async def _forecast_answer(
        self, state: _State, call: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """Read B2's forecast for the request's household. Read only: nothing is written."""
        if self._forecast is None:
            return FORECAST_NOT_AVAILABLE, None
        household_id = state["household_id"]
        reorder = call["name"] == "get_reorder_candidates"
        try:
            if reorder:
                summary = await self._forecast.reorder_candidates(household_id, self._clock())
            else:
                forecasts = await self._forecast.forecast_household(household_id, self._clock())
            items = await self._matcher.household_items(household_id)
        except Exception:  # B2's forecast or the history read failed; nothing was written
            logger.warning("Forecast failed for %s", call["name"], exc_info=True)
            return FORECAST_FAILED, None
        if reorder:
            view, card = _reorder_view(items, summary)
            return json.dumps(view, ensure_ascii=False), card

        item_name = str((call.get("args") or {}).get("item_name") or "").strip()
        if not item_name:
            return NO_FORECAST_ITEM, None
        match = match_term(item_name, items)
        if match.ambiguous:
            choices = [{"item_id": i.item_id, "item_name": i.name} for i in match.items]
            return json.dumps(
                {"item_name": item_name, "choices": choices}, ensure_ascii=False
            ), None
        item = match.matched
        forecast = next((f for f in forecasts if item and f.item_id == item.item_id), None)
        if item is None or forecast is None:
            not_found = {
                "item_name": item_name,
                "found": False,
                "household_items": [_household_item_view(i) for i in items],
            }
            return json.dumps(not_found, ensure_ascii=False), None
        row = _forecast_row(item, forecast)
        card = {"kind": "item_forecast", **row} if row["status"] == "estimated" else None
        return json.dumps(row, ensure_ascii=False), card

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

    async def _search_prices(self, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Run A2's price search. Read only: no price or option is stored."""
        item_name = str(args.get("item_name") or "").strip()
        if not item_name:
            return NO_ITEM_NAME, None
        if self._commerce is None:
            return PRICE_NOT_SUPPORTED, None
        try:
            options = await self._commerce.search_options(item_name, limit=MAX_PURCHASE_OPTIONS)
        except Exception:  # A2's provider failed; nothing was read or written
            logging.getLogger(__name__).warning(
                "Price search failed for %r", item_name, exc_info=True
            )
            return PRICE_FAILED, None
        model_view, data = _price_view(item_name, options[:MAX_PURCHASE_OPTIONS])
        return json.dumps(model_view, ensure_ascii=False), (data if options else None)

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
        for card in final.get("forecasts", []):
            if card["kind"] == "reorder_candidates":
                summary = "快用完的東西（預估，僅供參考，不會改庫存）"
            else:
                summary = f"{card['name']} 預估還能用約 {card['days_left']} 天（僅供參考）"
            results.append(AgentResult(kind=ResultKind.FORECAST, summary=summary, data=card))
        if "vision" in final:
            results.append(
                AgentResult(
                    kind=ResultKind.VISION,
                    summary="照片辨識結果（尚未寫入庫存）",
                    data=final["vision"],
                )
            )
        for found in final.get("purchase_options", []):
            demo = "示範資料，不是即時價格" if found["demo"] else "目前找到的價格"
            results.append(
                AgentResult(
                    kind=ResultKind.PURCHASE_OPTIONS,
                    summary=f"{found['item_name']} 比價結果（{demo}，僅供參考、不會存起來）",
                    data=found,
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

    from app.api.forecast import get_transaction_history
    from app.services.forecast import ForecastService

    inventory = get_inventory_service()
    tools = QueryTools(inventory, get_shopping_service())
    return ReadAgent(
        build_chat_model(settings),
        tools,
        load_system_prompt(),
        # Same ForecastService and history source as the `/forecast` routes (B2).
        forecast=ForecastService(inventory, get_transaction_history()),
        vision=get_vision_service(),
        media=get_media_storage_service(),
        # Built after load_azure_settings() read backend/.env, so BigGo keys there apply.
        commerce=BigGoCommerceProvider(),
    )
