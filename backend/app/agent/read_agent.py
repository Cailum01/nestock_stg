"""A1 Agent (Day 5, photo reading Day 8): understand, look, answer.

- Read tools: inventory, shopping list, photo recognition and price search.
- Two card tools, offered only when approval cards are configured: `complete_purchase`
  (v8 flow C, `app/agent/cards.py`) and `add_stock` (a new item the user says they
  got that is not a Shopping List entry, Issue #168, `app/agent/stock_cards.py`).
  Each stores a pending confirmation card and writes nothing else; the write happens
  in `/agent/resume` after the user confirms (AGENTS.md HITL rules 2 and 8).
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
import os
import re
from collections import Counter
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Protocol, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agent.cards import (
    CARD_CHANGED,
    ONE_CARD_PER_MESSAGE,
    OPEN_CARD_FIRST,
    OPEN_CARDS_UNKNOWN,
    PHOTO_CARD_MADE,
    PHOTO_KEPT,
    PHOTO_NOT_READY,
    PHOTO_SEEN,
    PURCHASE_TOOL_SCHEMA,
    CardChangeRefused,
    CardPhoto,
    PhotoCards,
    PhotoItem,
    PurchaseCards,
)
from app.agent.stock_cards import (
    ADD_STOCK,
    INCREASE_CARD_NOTE,
    STOCK_INCREASE_OFF,
    STOCK_INCREASE_VAR,
    StockCards,
    StockIncreases,
)
from app.core.dependencies import (
    get_approval_service,
    get_commerce_service,
    get_cosmos_read_repository,
    get_inventory_service,
    get_media_storage_service,
    get_shopping_service,
    get_vision_service,
)
from app.models.agent import AgentRequest, AgentResponse, AgentResult, ResultKind
from app.models.commerce import PurchaseOption
from app.models.domain import InventoryItem, MediaProcessingStatus, ShoppingItem
from app.models.vision import SceneType, VisionAnalysisResult
from app.services.approvals import (
    ApprovalRecord,
    ApprovalService,
    CardNotChangeable,
    NewOperation,
)
from app.services.item_matching import ItemMatch, ItemMatcher, MatchMethod, match_term
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.vision import VisionService
from app.tools.queries import QueryTools

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "agent_system.md"
MAX_STEPS = 8  # graph steps per message; stops endless tool loops
TOO_MANY_STEPS = "這個問題我查了太多次還是答不出來，請換個方式問問看。"
"""Reply when a message stops at `MAX_STEPS` with no card in it."""
CARD_MADE_STOPPED = "確認卡做好了，請看卡上的內容再按確認或取消。"
"""Reply when a message stops at `MAX_STEPS` after making a card (PR #216, Issue #213).

The card was stored before the stop, so it still goes to the App with this fixed text,
instead of `TOO_MANY_STEPS` with no card (a card the user cannot see, which still blocks
new cards until it expires).
"""
MAX_PURCHASE_OPTIONS = 5
DATED_PURCHASES_VAR = "NESTOCK_DATED_PURCHASES"  # "1": purchase day on (section 11)
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
CHANGING_CARD = (
    "使用者在下面這張確認卡上按了「送出修改」，這則訊息只寫了改過的地方。"
    "請照改過的內容，用原卡上的 shopping_item_id 重新呼叫 complete_purchase 出一張新卡；"
    "原卡上沒改到的品項和值照舊放進新卡。不要再問卡上已經有的資訊。原本的卡：{card}"
)
"""Given to the model with a 送出修改 message (contract section 7, Issue #91)."""
OPEN_CARD = (
    "使用者還有沒確認的確認卡，這次的回覆會再把最新的一張顯示出來：{card}。"
    "先回答使用者這則訊息，再用一句話提醒這張卡還沒確認，可以確認或取消。{more}"
    "這則訊息不要再出新卡：使用者說又買了東西，請他先確認或取消這張卡；"
    "使用者要改這張卡上的值，請他在卡上改好，再按「送出修改」。"
)
"""Given to the model when an open card is shown again (contract section 8, Issue #91)."""
OPEN_CARD_MORE = (
    "總共還有 {count} 張沒處理，這張處理完，下一則訊息會再顯示下一張；提醒時請說還有幾張。"
)
OPEN_CARD_PENDING = "open_card_pending"
"""`results[].data.code` for an open card shown again or when the App opens."""
OPEN_CARD_REMINDER = "上一張卡還沒處理，處理完才會出新卡。"
"""Fixed App-facing reminder; `data.open_cards` carries the count."""
CARD_CHANGE_REFUSED = {
    "not_found": "找不到這張確認卡，請再說一次要記錄的內容。",
    "expired": "這張確認卡已經過期，請再說一次要記錄的內容。",
    "confirmed": "這張確認卡已經確認了，不能再修改。",
    "completed": "這張確認卡已經記錄好了，不能再修改。",
    "cancelled": "這張確認卡已經取消了，請再說一次要記錄的內容。",
}
CARD_CHANGE_UNAVAILABLE = "現在不能修改確認卡，請再說一次要記錄的內容。"
CARD_CHANGE_NOT_DONE = (
    "這次修改沒有完成。請直接確認或取消原本那張卡；要改的話，取消後用說的再講一次。"
)
"""The old card could not be cancelled, so the new one was withdrawn (section 7)."""
LOW_CONFIDENCE = 0.6  # below this, the Agent says it is unsure (Issue #17)
NO_PHOTO = "這次沒有附照片，無法辨識。"
PHOTO_FAILED = json.dumps({"error": "photo_analysis_failed"})
"""Tool result when A2's recognition raises (timeout, upstream error): the Agent still
answers and asks for another photo instead of the whole request failing (502)."""
SHOPPING_LIST_UNAVAILABLE = "shopping_list_unavailable"
"""Set when an inventory lookup could not read the shopping list. The inventory is still
answered; the list is never passed on as empty (C1 review on PR #57)."""

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
            "description": (
                "查詢家裡目前的庫存：品名、數量、單位、放置位置、別名。"
                "會一起附上待購清單裡還沒買的東西。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {
                        "type": "string",
                        "description": (
                            "使用者問某一樣東西時填：使用者說的品名（例如衛生紙）。"
                            "問家裡有什麼、要看整份庫存時不要填。"
                        ),
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_shopping",
            "description": "查詢待購清單：還要買的東西和狀態。",
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
    pending_approval: dict[str, Any]
    replaces_approval_id: str
    """The card a 送出修改 replaces (section 7); the new card records it."""
    open_card_shown: bool
    """An open card is shown again with this reply (section 8); no new card is made."""
    open_cards_unknown: bool
    """The open cards could not be listed (section 8); no new card is made."""
    photo_card_made: bool
    """`analyze_photo` made a card in this message: the model speaks once more, with no
    tools, and the message ends (PR #216, Issue #213)."""


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


def _change_refused(
    code: str, message: str, *, card: dict[str, Any] | None = None, **details: Any
) -> AgentResponse:
    """A refused 送出修改 sent as data: 200, the code in `results[0].data` (Issue #162)."""
    return AgentResponse(
        message=message,
        results=[
            AgentResult(
                kind=ResultKind.ACTION_RESULT, summary=message, data={"code": code, **details}
            )
        ],
        pending_approval=card,
    )


def _open_card_reminder(count: int) -> AgentResult:
    """Fixed App-facing reminder for an open card (approval lifecycle section 8)."""
    return AgentResult(
        kind=ResultKind.ACTION_RESULT,
        summary=OPEN_CARD_REMINDER,
        data={"code": OPEN_CARD_PENDING, "open_cards": count},
    )


def _no_new_card(state: dict[str, Any], update: dict[str, Any]) -> str | None:
    """Why a card tool makes no card now (section 8), or None when it may."""
    if state.get("open_cards_unknown"):
        return OPEN_CARDS_UNKNOWN
    if state.get("open_card_shown"):
        return OPEN_CARD_FIRST
    if "pending_approval" in update or "pending_approval" in state:
        return ONE_CARD_PER_MESSAGE
    return None


def _card_view(record: ApprovalRecord) -> str:
    """The card being changed, as the model needs it: lines and their shown values."""
    lines = [
        {
            "action": operation.action,
            "description": operation.description,
            "values": {k: v for k, v in operation.values.items() if k != "transaction_id"},
        }
        for operation in record.operations
    ]
    return json.dumps(lines, ensure_ascii=False)


def _household_item_view(item: InventoryItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "name": item.name,
        "aliases": list(item.aliases or []),
        "unit": item.unit,
    }


def _shopping_view(item: ShoppingItem) -> dict[str, Any]:
    """A shopping entry as the model sees it, without its planned quantity (Issue #179).

    The App's shopping list shows only the items, so the Agent must not read out a
    planned quantity the user never sees. The field stays in the backend (Issue #180).
    """
    row = item.model_dump(mode="json")
    row.pop("quantity", None)
    return row


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


def _new_photo_items(result: VisionAnalysisResult, matches: list[ItemMatch]) -> list[PhotoItem]:
    """Photo items for a new-item card (Issue #124).

    Only items with no household item of the same name or alias, seen with confidence
    (not `low_confidence`) and with a confirmed unit. Others are only reported.
    """
    items = []
    for detected, match in zip(result.detected_items, matches, strict=True):
        unit = (detected.unit or "").strip()
        if (
            match.method is MatchMethod.NONE
            and detected.confidence >= LOW_CONFIDENCE
            and detected.unit_confirmed
            and unit
        ):
            items.append(
                PhotoItem(name=detected.canonical_name, quantity=detected.quantity, unit=unit)
            )
    return items


def _seen_household_items(
    result: VisionAnalysisResult, matches: list[ItemMatch]
) -> list[tuple[ItemMatch, int, str]]:
    """Photo items that are one household item, to add to it (Issue #206).

    (the match, quantity, unit) for items seen with confidence and a confirmed unit
    whose name or alias is exactly one household item's. Others are only reported.
    """
    seen = []
    for detected, match in zip(result.detected_items, matches, strict=True):
        unit = (detected.unit or "").strip()
        if (
            match.matched is not None
            and detected.confidence >= LOW_CONFIDENCE
            and detected.unit_confirmed
            and unit
        ):
            seen.append((match, detected.quantity, unit))
    return seen


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


_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+")


def _plain(text: str) -> str:
    """The App prints the reply as typed, so Markdown bold and headings show as symbols.

    The prompt asks for plain text; this removes what the model adds anyway (A1-89,
    seen in Issue #124's screenshot): `**` pairs and `#` at the start of a line.
    """
    return _HEADING.sub("", text.replace("**", ""))


class ReadAgent:
    """Implements AgentRuntime.respond. Cards it makes are answered in `/agent/resume`."""

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
        purchases: PurchaseCards | None = None,
        photo_cards: PhotoCards | None = None,
        stock: StockCards | None = None,
        increases: StockIncreases | None = None,
    ) -> None:
        card_tools = [cards.tool_schema for cards in (purchases, stock) if cards is not None]
        self._model = model.bind_tools([*TOOL_SCHEMAS, *card_tools])
        # After a photo card the model only speaks (PR #216): the same model, no tools.
        self._speaker = model
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
        self._purchases = purchases
        # Issue #124: new-item cards from photos; off in the app until they can be written.
        self._photo_cards = photo_cards
        self._increases = increases  # Issue #206: add to household items a photo shows
        # Issue #168: new-item cards for what the user says they got (`add_stock`).
        self._stock = stock
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
                    # Section 8: no photo card with an open card shown again, nor when
                    # the open cards could not be listed (unknown is not "none").
                    carded = (
                        "pending_approval" in update
                        or "pending_approval" in state
                        or bool(state.get("open_cards_unknown"))
                    )
                    content, vision, card = await self._analyze(
                        state, call.get("args") or {}, may_make_card=not carded
                    )
                    if vision is not None:
                        update["vision"] = vision
                    if card is not None:
                        update["pending_approval"] = card
                        update["photo_card_made"] = True
                elif call["name"] == "search_purchase_options":
                    content, found = await self._search_prices(call.get("args") or {})
                    if found is not None:
                        earlier = update.get("purchase_options", state.get("purchase_options", []))
                        update["purchase_options"] = [*earlier, found]
                elif call["name"] == "list_inventory":
                    content, rows = await self._inventory_answer(
                        state["household_id"], call.get("args") or {}
                    )
                    if rows is not None:
                        update["inventory"] = rows
                elif call["name"] == PURCHASE_TOOL_SCHEMA["function"]["name"]:
                    content, card = await self._propose_purchase(state, update, call)
                    if card is not None:
                        update["pending_approval"] = card
                elif call["name"] == ADD_STOCK:
                    content, card = await self._propose_stock(state, update, call)
                    if card is not None:
                        update["pending_approval"] = card
                elif call["name"] == "list_shopping":
                    items = await self._handlers["list_shopping"](state["household_id"])
                    content = json.dumps([_shopping_view(i) for i in items], ensure_ascii=False)
                elif handler is None:
                    content = f"沒有這個工具：{call['name']}"
                else:
                    items = await handler(state["household_id"])
                    rows = [item.model_dump(mode="json") for item in items]
                    content = json.dumps(rows, ensure_ascii=False)
                update["messages"].append(ToolMessage(content, tool_call_id=call["id"]))
            return update

        async def speak(state: _State) -> _State:
            # PR #216, Issue #213: the photo card is made; the model tells the user about
            # it (photo kept or not, items only reported, what was asked) but cannot call
            # tools again, so it cannot run into MAX_STEPS and lose the card.
            reply = await self._speaker.ainvoke(state["messages"])
            return {"messages": [reply]}

        def next_step(state: _State) -> str:
            return "look" if state["messages"][-1].tool_calls else END

        def after_look(state: _State) -> str:
            return "speak" if state.get("photo_card_made") else "think"

        builder = StateGraph(_State)
        builder.add_node("think", think)
        builder.add_node("look", look)
        builder.add_node("speak", speak)
        builder.add_edge(START, "think")
        builder.add_conditional_edges("think", next_step, ["look", END])
        builder.add_conditional_edges("look", after_look, ["think", "speak"])
        builder.add_edge("speak", END)
        return builder.compile()

    async def _inventory_answer(
        self, household_id: str, args: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """(result for the model, rows for the App's inventory card).

        With an `item_name`, only the household items with that name or alias are
        returned, so a question about 衛生紙 shows 衛生紙 and nothing else (DEMO
        checklist v3, section 3 B). When nothing has that name, the model gets the
        household's item names to judge another name for the same thing (AA 電池 and
        電池) and asks again; no card is shown for that lookup. Without a name, the
        whole inventory is returned as before.
        """
        items = await self._handlers["list_inventory"](household_id)
        item_name = str(args.get("item_name") or "").strip()
        if not item_name:
            rows = [item.model_dump(mode="json") for item in items]
            return await self._with_shopping_list(household_id, {"inventory": rows}), rows
        match = match_term(item_name, items)
        if not match.items:
            view: dict[str, Any] = {
                "item_name": item_name,
                "found": False,
                "household_items": [_household_item_view(item) for item in items],
            }
            return await self._with_shopping_list(household_id, view), None
        rows = [item.model_dump(mode="json") for item in match.items]
        view = {"item_name": item_name, "inventory": rows}
        return await self._with_shopping_list(household_id, view), rows

    async def _with_shopping_list(self, household_id: str, view: dict[str, Any]) -> str:
        """v8 Flow B: an inventory lookup also shows what is already on the shopping list.

        If the list cannot be read (for example Cosmos is unreachable), the inventory is
        still answered and the model is told the list is unavailable. A failed read is
        never passed on as an empty list.
        """
        try:
            shopping = await self._handlers["list_shopping"](household_id)
        except Exception:  # only the extra read failed; the inventory was read
            logger.warning("Shopping list read failed during an inventory lookup", exc_info=True)
            view = {
                **view,
                "shopping_list": None,
                "shopping_list_error": SHOPPING_LIST_UNAVAILABLE,
            }
        else:
            pending = [_shopping_view(item) for item in shopping if item.status == "pending"]
            view = {**view, "shopping_list": pending}
        return json.dumps(view, ensure_ascii=False)

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
        self, state: _State, args: dict[str, Any], *, may_make_card: bool = True
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
        """Run A2's recognition on the request's own (already verified) photo.

        (result for the model, data for the App, new-item card or None). With photo
        cards on, items the household has no item for get one new-item card (Issue #124).
        """
        image_ref = state.get("image_ref")
        if not image_ref or self._vision is None:
            return NO_PHOTO, None, None
        household_id = state["household_id"]
        try:
            result = await self._vision.analyze_image(
                household_id, image_ref, _scene(args.get("scene_type"))
            )
        except Exception:  # A2's recognition failed; nothing was read or written
            logger.warning("Photo analysis failed for image_ref=%s", image_ref, exc_info=True)
            return PHOTO_FAILED, None, None
        household_items = await self._matcher.household_items(household_id)
        matches = [
            match_term(detected.canonical_name, household_items)
            for detected in result.detected_items
        ]
        model_view, data = _vision_view(result, matches, household_items)
        card = None
        if self._photo_cards is not None and may_make_card:
            new_items = _new_photo_items(result, matches)
            more = await self._seen_increases(household_id, result, matches)
            photo = await self._card_photo(state) if new_items else None
            card = await self._photo_cards.propose(household_id, new_items, photo, more)
            if card is not None and new_items:
                model_view["new_item_card"] = {
                    "lines": [operation["description"] for operation in card["operations"]],
                    "note": PHOTO_CARD_MADE + (INCREASE_CARD_NOTE if more else ""),
                    "photo": PHOTO_KEPT if photo is not None and photo.ready else PHOTO_NOT_READY,
                }
            elif card is not None:
                # Only items the household has (Issue #206): no new item, no photo kept.
                model_view["stock_increase_card"] = {
                    "lines": [operation["description"] for operation in card["operations"]],
                    "note": INCREASE_CARD_NOTE,
                }
        return json.dumps(model_view, ensure_ascii=False), data, card

    async def _seen_increases(
        self, household_id: str, result: VisionAnalysisResult, matches: list[ItemMatch]
    ) -> list[NewOperation]:
        """Lines that add what the photo shows to items the household has (Issue #206).

        None while that is off. A household item the photo shows once gets a line. One
        the photo shows twice gets no line at all: adding only one of the sightings
        would record the wrong quantity (PR #207 B1 review). Such an item, one of several
        household items of a name, and one whose unit differs or that cannot be read are
        only reported.
        """
        if self._increases is None:
            return []
        seen: list[NewOperation] = []
        for match, quantity, unit in _seen_household_items(result, matches):
            line, _ = await self._increases.line(
                household_id, match, quantity=quantity, unit=unit, source=PHOTO_SEEN
            )
            if line is not None:
                seen.append(line)
        times = Counter(line.values["item_id"] for line in seen)
        return [line for line in seen if times[line.values["item_id"]] == 1]

    async def _card_photo(self, state: _State) -> CardPhoto | None:
        """The request's photo for a card to keep (Issue #176), or None without a photo.

        Read after recognition, so the card holds the mapping as it is when the card is
        made: `input_id` and its ETag come from the mapping only when it is completed and
        is this household's; otherwise the photo is not `ready` and no card keeps it.
        """
        image_ref = state.get("image_ref")
        if not image_ref or self._media is None:
            return None
        household_id = state["household_id"]
        try:
            snapshot = await self._media.get_media_snapshot(household_id, image_ref)
        except Exception:  # the mapping could not be read; the card keeps no photo
            logger.warning(
                "Photo mapping could not be read for image_ref=%s", image_ref, exc_info=True
            )
            snapshot = None
        if (
            snapshot is None
            or not snapshot.etag
            or snapshot.record.status is not MediaProcessingStatus.COMPLETED
            or snapshot.record.household_id != household_id
            or snapshot.record.image_ref != image_ref
        ):
            return CardPhoto(image_ref=image_ref)
        return CardPhoto(
            image_ref=image_ref, input_id=snapshot.record.input_id, mapping_etag=snapshot.etag
        )

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

    async def _propose_purchase(
        self, state: _State, update: _State, call: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """Make the purchase card. One card per message; it writes nothing else."""
        if self._purchases is None:
            return f"沒有這個工具：{call['name']}", None
        refused = _no_new_card(state, update)
        if refused is not None:
            return refused, None
        return await self._purchases.propose(
            state["household_id"],
            call.get("args") or {},
            replaces_approval_id=state.get("replaces_approval_id"),
            photo=await self._card_photo(state),
        )

    async def _propose_stock(
        self, state: _State, update: _State, call: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """Make the new-item card for what the user says they got (Issue #168).

        The same rules as the purchase card: one card per message, and no new card while
        an open card is shown again or the open cards are unknown (section 8).
        """
        if self._stock is None:
            return f"沒有這個工具：{call['name']}", None
        refused = _no_new_card(state, update)
        if refused is not None:
            return refused, None
        return await self._stock.propose(
            state["household_id"],
            call.get("args") or {},
            replaces_approval_id=state.get("replaces_approval_id"),
        )

    async def _change_card(self, request: AgentRequest) -> AgentResponse:
        """送出修改 sent as data (Issue #162, contract section 7): no model call.

        The reply is 200 either way. A refused change carries `results[0].data.code`
        and, while the old card is still pending, that card again, so it stays on
        screen; nothing new is stored. A photo new-item card is changed by
        `PhotoCards` (Issue #124), only while photo cards are on; with them off it is
        refused like any card that cannot be changed on the card.
        """
        household_id = request.household_id
        if self._purchases is None:
            return _change_refused("card_not_changeable", CARD_CHANGE_UNAVAILABLE)
        try:
            old = await self._purchases.card_to_change(household_id, request.replaces_approval_id)
        except CardNotChangeable as refused:
            return _change_refused(
                "card_not_changeable", CARD_CHANGE_REFUSED[refused.reason], reason=refused.reason
            )
        changes = request.card_changes or []
        try:
            if self._photo_cards is not None and PhotoCards.made(old):
                # A photo or said card (Issue #124, #168): name, quantity and location
                # (Issue #188), checked against today's items.
                items = await self._matcher.household_items(household_id)
                card = await self._photo_cards.change_card(old, changes, items)
            else:
                card = await self._purchases.change_card(old, changes)
        except CardChangeRefused as refused:
            shown = ApprovalService.to_pending_approval(old).model_dump(mode="json")
            return _change_refused(refused.code, refused.message, card=shown, **refused.details)
        not_replaced = await self._purchases.retire(
            household_id, old.approval_id, card["approval_id"]
        )
        if not_replaced is not None:
            return _change_refused(
                "card_not_changeable",
                CARD_CHANGE_REFUSED.get(not_replaced, CARD_CHANGE_NOT_DONE),
                reason=not_replaced,
            )
        return AgentResponse(
            message=CARD_CHANGED,
            results=[
                AgentResult(
                    kind=ResultKind.ACTION_RESULT,
                    summary=CARD_CHANGED,
                    data={"code": "card_changed", "replaced_approval_id": old.approval_id},
                )
            ],
            pending_approval=card,
        )

    async def open_card(self, household_id: str) -> AgentResponse | None:
        """Return the newest unanswered card without invoking the model.

        The lookup has the same expiry handling as the message path. None means open
        cards could not be listed; an empty response means there is no open card.
        """
        if self._purchases is None:
            return AgentResponse(message="")
        open_cards = await self._purchases.open_cards(household_id)
        if open_cards is None:
            return None
        if not open_cards:
            return AgentResponse(message="")
        reminder = _open_card_reminder(len(open_cards))
        return AgentResponse(
            message=reminder.summary,
            results=[reminder],
            pending_approval=ApprovalService.to_pending_approval(open_cards[0]).model_dump(
                mode="json"
            ),
        )

    async def respond(self, request: AgentRequest) -> AgentResponse:
        if request.card_changes is not None:
            return await self._change_card(request)
        messages: list[AnyMessage] = [SystemMessage(self._system_prompt)]
        if self._purchases is not None and (today := self._purchases.date_note()):
            messages.append(SystemMessage(today))  # dated purchases only (A1-50); off
        if self._stock is not None and (adding := self._stock.prompt_note()):
            messages.append(SystemMessage(adding))  # Issue #206; off until its write exists
        start: _State = {"household_id": request.household_id}
        if request.image_ref and self.reads_photos:
            # D-5: the photo must belong to this household before anything reads it.
            if not await self._media.verify_media_exists(request.household_id, request.image_ref):
                return AgentResponse(message=PHOTO_NOT_FOUND)
            start["image_ref"] = request.image_ref
            messages.append(SystemMessage(PHOTO_ATTACHED))
        elif request.image_ref:  # accepted, not read: no vision service configured
            messages.append(SystemMessage(PHOTO_NOT_SUPPORTED))
        replacing = None
        open_count = 0
        if request.replaces_approval_id:
            if self._purchases is None:
                return AgentResponse(message=CARD_CHANGE_UNAVAILABLE)
            try:
                replacing = await self._purchases.card_to_change(
                    request.household_id, request.replaces_approval_id
                )
            except CardNotChangeable as refused:
                return AgentResponse(message=CARD_CHANGE_REFUSED[refused.reason])
            start["replaces_approval_id"] = replacing.approval_id
            messages.append(SystemMessage(CHANGING_CARD.format(card=_card_view(replacing))))
        elif self._purchases is not None:
            # Section 8: a card cleared from the screen is still open; show the newest
            # one again (same approval_id) instead of a reminder the user cannot press.
            open_cards = await self._purchases.open_cards(request.household_id)
            if open_cards is None:
                start["open_cards_unknown"] = True  # unknown is not "none": no new card
            elif open_cards:
                shown = open_cards[0]
                card = ApprovalService.to_pending_approval(shown).model_dump(mode="json")
                start["pending_approval"] = card
                start["open_card_shown"] = True
                open_count = len(open_cards)
                more = OPEN_CARD_MORE.format(count=len(open_cards)) if len(open_cards) > 1 else ""
                messages.append(SystemMessage(OPEN_CARD.format(card=_card_view(shown), more=more)))
        messages.append(HumanMessage(request.message))
        start["messages"] = messages
        final: _State = start
        stopped = False
        try:
            # The state after every step is kept, so a stop at MAX_STEPS still knows
            # whether this message stored a card (PR #216, Issue #213).
            async for final in self._graph.astream(
                start, {"recursion_limit": MAX_STEPS}, stream_mode="values"
            ):
                pass
        except GraphRecursionError:
            stopped = True
        if stopped and final.get("pending_approval") is None:
            return AgentResponse(message=TOO_MANY_STEPS)
        if replacing is not None and final.get("pending_approval") is not None:
            # Section 7: the old card is cancelled only after the new one is stored. Unless
            # it ends up cancelled as replaced, the new card is withdrawn and not shown, and
            # it never counts: never two cards that can be confirmed.
            not_replaced = await self._purchases.retire(
                request.household_id,
                replacing.approval_id,
                final["pending_approval"]["approval_id"],
            )
            if not_replaced is not None:
                return AgentResponse(
                    message=CARD_CHANGE_REFUSED.get(not_replaced, CARD_CHANGE_NOT_DONE)
                )

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
        in_reply = final.get("pending_approval")
        if open_count and in_reply is not None and in_reply == start.get("pending_approval"):
            results.append(_open_card_reminder(open_count))
        if not stopped:
            message = _plain(_text(final["messages"][-1]))
        elif in_reply == start.get("pending_approval"):
            message = OPEN_CARD_REMINDER  # the open card shown again; no new card made
        else:
            message = CARD_MADE_STOPPED
        return AgentResponse(
            message=message,
            results=results,
            pending_approval=final.get("pending_approval"),
        )


@lru_cache(maxsize=1)
def get_read_agent() -> ReadAgent | None:
    """FastAPI dependency. None when Vertex is unconfigured (route answers 501)."""
    from app.agent.llm import build_chat_model, load_vertex_settings

    settings = load_vertex_settings()
    if settings is None:
        return None

    from app.api.forecast import get_transaction_history
    from app.services.forecast import ForecastService

    inventory = get_inventory_service()
    tools = QueryTools(inventory, get_shopping_service())
    increases = _stock_increases()
    return ReadAgent(
        build_chat_model(settings),
        tools,
        load_system_prompt(),
        # Same ForecastService and history source as the `/forecast` routes (B2).
        forecast=ForecastService(inventory, get_transaction_history()),
        vision=get_vision_service(),
        media=get_media_storage_service(),
        commerce=get_commerce_service(),
        purchases=_purchase_cards(),
        # Issue #124: photo new-item cards, written on confirm by B1's no-Shopping
        # writer through A1's `CardWriteRouter` (`get_approval_service`, PR #171).
        photo_cards=_photo_cards(),
        # Issue #168: said new-item cards, written the same way as photo cards.
        # Issue #206: the same lines for household items, said or in a photo; on
        # unless switched off (`stock_increase_on`). Made once, for both.
        stock=_stock_cards(increases),
        increases=increases,
    )


def _photo_cards() -> PhotoCards | None:
    """Photo new-item cards need approval storage, like purchase cards: Cosmos only."""
    approvals = get_approval_service()
    if approvals is None:
        return None
    return PhotoCards(approvals)


def _stock_cards(increases: StockIncreases | None) -> StockCards | None:
    """The `add_stock` tool needs approval storage and the inventory list: Cosmos only."""
    approvals = get_approval_service()
    reader = get_cosmos_read_repository()
    if approvals is None or reader is None:
        return None
    return StockCards(reader, approvals, increases)


def _stock_increases() -> StockIncreases | None:
    """Adding to an item the household has (Issue #206): Cosmos only, on by default.

    `NESTOCK_STOCK_INCREASE=0` (or `false`, `off`, `no`) turns it off; a household
    item is then left out of the card as before. Its write is B1's and C1's, in
    `get_approval_service`.
    """
    reader = get_cosmos_read_repository()
    if reader is None or not stock_increase_on():
        return None
    return StockIncreases(reader)


def stock_increase_on(environment: Mapping[str, str] | None = None) -> bool:
    """Adding to household items is on unless switched off (Issue #206).

    `NESTOCK_STOCK_INCREASE` set to `0`, `false`, `off` or `no` (any case) turns it
    off, so whichever of them someone types in an emergency works (PR #211 B2
    review). Unset, or any other value, is on: nobody has to set anything for it to
    work, and turning it off needs no code change.
    """
    env = os.environ if environment is None else environment
    return env.get(STOCK_INCREASE_VAR, "").strip().lower() not in STOCK_INCREASE_OFF


def _purchase_cards() -> PurchaseCards | None:
    """The card tool needs approval storage and versioned reads: Cosmos only."""
    approvals = get_approval_service()
    reader = get_cosmos_read_repository()
    if approvals is None or reader is None:
        return None
    # Read after the services above loaded backend/.env, so a line there applies.
    return PurchaseCards(reader, approvals, dated_purchases=dated_purchases_on())


def dated_purchases_on(environment: Mapping[str, str] | None = None) -> bool:
    """`NESTOCK_DATED_PURCHASES=1` turns the purchase day on (contract section 11).

    For testing on your own computer; the cloud does not set it, so it stays off there.
    Any other value, or none, is off.
    """
    env = os.environ if environment is None else environment
    return env.get(DATED_PURCHASES_VAR, "").strip() == "1"
