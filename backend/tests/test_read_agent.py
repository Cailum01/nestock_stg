"""A1 read-only Agent tests (Day 5). A scripted fake model; no network, no Azure cost."""

import asyncio
import json
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.read_agent import TOOL_SCHEMAS, ReadAgent, get_read_agent, load_system_prompt
from app.core.dependencies import get_inventory_service, get_shopping_service
from app.main import create_app
from app.models.agent import AgentRequest, ResultKind
from app.models.commerce import PurchaseOption
from app.models.domain import InventoryItem
from app.models.vision import DetectedItem, MatchStatus, SceneType, VisionAnalysisResult
from app.services.commerce.biggo import BigGoCommerceProvider
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.vision import MockVisionService
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
    assert names == {
        "list_inventory",
        "list_shopping",
        "get_reorder_candidates",
        "get_consumption_forecast",
        "analyze_photo",
        "search_purchase_options",
    }


def test_inventory_question_looks_then_answers():
    model = ScriptedModel(_call("list_inventory"), AIMessage(content="還有 2 包白米，在廚房櫃子。"))
    response = _ask(_agent(model), "家裡還有白米嗎？")

    assert response.message == "還有 2 包白米，在廚房櫃子。"
    assert response.pending_approval is None
    seen = _tool_rows(model)  # the model's second turn saw the real service data
    assert [row["name"] for row in seen["inventory"]] == ["白米"]
    assert response.results[0].kind == ResultKind.INVENTORY
    assert response.results[0].data["items"][0]["location"] == "廚房櫃子"


def test_shopping_question_uses_shopping_list():
    model = ScriptedModel(_call("list_shopping"), AIMessage(content="還要買 1 瓶牛奶。"))
    response = _ask(_agent(model), "我還要買什麼？")

    assert [row["name"] for row in _tool_rows(model)] == ["牛奶"]
    assert response.results == []  # inventory card only when inventory was looked up


def test_the_model_never_sees_a_planned_shopping_quantity():
    # Issue #179: the App's shopping list shows only items, so the Agent must not read
    # out a planned quantity; the unit stays for the purchase card's unit rules.
    model = ScriptedModel(_call("list_shopping"), AIMessage(content="還要買牛奶。"))
    _ask(_agent(model), "我還要買什麼？")

    rows = _tool_rows(model)
    assert rows and all("quantity" not in row for row in rows)
    assert all("unit" in row and "name" in row for row in rows)


def test_an_inventory_lookup_shows_the_shopping_list_without_planned_quantities():
    model = ScriptedModel(_call("list_inventory"), AIMessage(content="庫存裡沒有找到 AA 電池。"))
    _ask(_agent(model), "我要買 AA 電池，家裡還有嗎？")

    shopping = _tool_rows(model)["shopping_list"]
    assert [row["name"] for row in shopping] == ["牛奶"]
    assert all("quantity" not in row for row in shopping)


def test_prompt_and_tool_say_the_shopping_list_has_no_quantities():
    assert "只說品項，不要說要買幾個" in load_system_prompt()
    tool = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "list_shopping")
    assert "數量" not in tool["function"]["description"]


# --- v8 Flow B: "I want to buy X, do we still have it?" ----------------------


def test_flow_b_lookup_shows_shopping_list_and_writes_nothing():
    # One inventory lookup is enough to also say "already on the shopping list".
    model = ScriptedModel(_call("list_inventory"), AIMessage(content="庫存裡沒有找到 AA 電池。"))
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: _agent(model)
    with TestClient(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]
        response = client.post(
            "/agent/messages",
            json={"household_id": HOUSEHOLD, "message": "我要買 AA 電池，家裡還有嗎？"},
        )
        after = [client.get(path).json() for path in ["/inventory", "/shopping"]]

    assert response.status_code == 200
    seen = _tool_rows(model)
    assert [row["name"] for row in seen["inventory"]] == ["白米"]
    assert [row["name"] for row in seen["shopping_list"]] == ["牛奶"]
    assert len(model.seen) == 2  # one lookup, then the answer
    body = response.json()
    assert body["pending_approval"] is None
    assert [result["kind"] for result in body["results"]] == ["inventory"]
    assert body["results"][0]["data"]["items"] == seen["inventory"]  # card: inventory only
    assert before == after


def test_flow_b_leaves_out_items_already_bought():
    from app.models.domain import ShoppingStatus

    class Shopping:
        async def list_items(self, household_id: str):
            milk = (await get_shopping_service().list_items(household_id))[0]
            bought = milk.model_copy(
                update={
                    "shopping_item_id": "bought",
                    "name": "衛生紙",
                    "status": ShoppingStatus.COMPLETED,
                }
            )
            return [milk, bought]

    model = ScriptedModel(_call("list_inventory"), AIMessage(content="庫存裡沒有找到衛生紙。"))
    tools = QueryTools(get_inventory_service(), Shopping())
    _ask(ReadAgent(model, tools, load_system_prompt()), "我要買衛生紙，家裡還有嗎？")

    assert [row["name"] for row in _tool_rows(model)["shopping_list"]] == ["牛奶"]


class _UnreadableShopping:
    """The shopping list cannot be read, for example because Cosmos is unreachable."""

    async def list_items(self, household_id: str):
        raise ConnectionError("Cosmos is unreachable")


def test_flow_b_still_answers_inventory_when_the_shopping_list_cannot_be_read():
    answer = "白米還有 2 包，在廚房櫃子；待購清單暫時查不到。"
    model = ScriptedModel(_call("list_inventory"), AIMessage(content=answer))
    tools = QueryTools(get_inventory_service(), _UnreadableShopping())

    response = _ask(ReadAgent(model, tools, load_system_prompt()), "我要買白米，家裡還有嗎？")

    seen = _tool_rows(model)
    assert [row["name"] for row in seen["inventory"]] == ["白米"]
    assert seen["shopping_list"] is None  # a failed read is never passed on as an empty list
    assert seen["shopping_list_error"] == "shopping_list_unavailable"
    assert response.message == answer
    assert response.results[0].kind == ResultKind.INVENTORY  # the inventory card is still shown
    assert response.results[0].data["items"] == seen["inventory"]


def test_prompt_says_how_to_answer_when_the_shopping_list_is_unavailable():
    prompt = load_system_prompt()
    assert "`shopping_list_error`" in prompt
    assert "待購清單暫時查不到" in prompt
    assert "不要說「不在待購清單上」" in prompt


# --- One item asked about: only that item comes back (DEMO checklist v3, 3 B) -----


def _item(item_id, name, quantity, unit, location, aliases=None):
    return InventoryItem(
        item_id=item_id,
        household_id=HOUSEHOLD,
        name=name,
        quantity=quantity,
        unit=unit,
        location=location,
        aliases=aliases,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        updated_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


class _Household:
    """A household with several items, like the DEMO baseline."""

    def __init__(self, *items):
        self.items = list(items) or [
            _item("seed-tissue", "衛生紙", 8, "包", "浴室櫃子", aliases=["面紙"]),
            _item("seed-detergent", "洗衣精", 1, "瓶", "陽台洗衣機旁"),
            _item("seed-battery", "電池", 4, "顆", "客廳抽屜"),
        ]

    async def list_items(self, household_id: str):
        return [item for item in self.items if item.household_id == household_id]


def _household_agent(model, inventory=None):
    tools = QueryTools(inventory or _Household(), get_shopping_service())
    return ReadAgent(model, tools, load_system_prompt())


def _tool_results(model):
    return [json.loads(m.content) for m in model.seen[-1] if isinstance(m, ToolMessage)]


def test_asking_about_one_item_shows_only_that_item():
    model = ScriptedModel(
        _call("list_inventory", item_name="衛生紙"),
        AIMessage(content="衛生紙還有 8 包，在浴室櫃子。"),
    )

    response = _ask(_household_agent(model), "衛生紙還有幾包？放在哪裡？")

    (seen,) = _tool_results(model)
    assert seen["item_name"] == "衛生紙"
    assert [row["name"] for row in seen["inventory"]] == ["衛生紙"]
    assert "shopping_list" in seen  # Flow B still sees the shopping list
    (card,) = response.results
    assert card.kind == ResultKind.INVENTORY
    assert [row["name"] for row in card.data["items"]] == ["衛生紙"]


def test_an_alias_finds_the_household_item():
    model = ScriptedModel(_call("list_inventory", item_name="面紙"), AIMessage(content="有"))

    response = _ask(_household_agent(model), "面紙還有嗎？")

    assert [row["name"] for row in response.results[0].data["items"]] == ["衛生紙"]


def test_another_name_gets_the_household_names_and_the_second_lookup_shows_one_item():
    model = ScriptedModel(
        _call("list_inventory", item_name="AA 電池"),
        _call("list_inventory", call_id="call-2", item_name="電池"),
        AIMessage(content="電池還有 4 顆，在客廳抽屜。"),
    )

    response = _ask(_household_agent(model), "我要買 AA 電池，家裡還有嗎？")

    first, second = _tool_results(model)
    assert first["found"] is False and "inventory" not in first
    assert [item["name"] for item in first["household_items"]] == ["衛生紙", "洗衣精", "電池"]
    assert "shopping_list" in first
    assert [row["name"] for row in second["inventory"]] == ["電池"]
    (card,) = response.results
    assert [row["name"] for row in card.data["items"]] == ["電池"]


def test_nothing_found_shows_no_inventory_card():
    model = ScriptedModel(
        _call("list_inventory", item_name="螺絲起子"),
        AIMessage(content="庫存裡沒有找到螺絲起子。"),
    )

    response = _ask(_household_agent(model), "螺絲起子放在哪裡？")

    assert response.results == []


def test_items_with_the_same_name_all_come_back():
    two = _Household(
        _item("soy-1", "醬油", 2, "瓶", "廚房調味架"),
        _item("soy-2", "醬油", 1, "瓶", "冰箱"),
        _item("salt", "鹽", 3, "包", "廚房調味架"),
    )
    model = ScriptedModel(_call("list_inventory", item_name="醬油"), AIMessage(content="有兩瓶"))

    response = _ask(_household_agent(model, two), "醬油放哪？")

    assert [row["item_id"] for row in response.results[0].data["items"]] == ["soy-1", "soy-2"]


def test_no_or_blank_item_name_returns_the_whole_inventory():
    for args in ({}, {"item_name": ""}, {"item_name": "  "}):
        model = ScriptedModel(_call("list_inventory", **args), AIMessage(content="這些"))

        response = _ask(_household_agent(model), "家裡有什麼？")

        assert len(response.results[0].data["items"]) == 3


def test_prompt_tells_the_model_when_to_name_the_item():
    prompt = load_system_prompt()
    assert "`item_name` 填使用者說的品名" in prompt
    assert "`found` 是 false" in prompt and "「AA 電池」和「電池」" in prompt


def test_markdown_symbols_never_reach_the_app():
    """Issue #124's screenshot showed Noki's `**` printed as-is in the App (A1-89)."""
    reply = "**衛生紙**還有 8 包。\n## 位置\n浴室櫃子，貨號 #1 那格"
    model = ScriptedModel(AIMessage(content=reply))

    response = _ask(_agent(model), "衛生紙在哪？")

    assert response.message == "衛生紙還有 8 包。\n位置\n浴室櫃子，貨號 #1 那格"


def test_prompt_asks_for_plain_text():
    assert "不要用 Markdown 符號" in load_system_prompt()


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
    assert _tool_rows(model) == {"inventory": [], "shopping_list": []}


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


def test_photo_is_accepted_but_not_pretended_to_be_seen():
    model = ScriptedModel(AIMessage(content="照片辨識還沒開放，目前只能用文字查詢。"))
    request = AgentRequest(
        household_id=HOUSEHOLD,
        message="幫我看看這張照片裡有什麼",
        image_ref="blob://households/demo-household/media/photo.jpg",
    )
    response = asyncio.run(_agent(model).respond(request))

    first_turn = model.seen[0]
    assert any("看不到照片內容" in str(m.content) for m in first_turn)
    assert len(model.seen) == 1  # answered directly, no tool loop
    assert response.pending_approval is None


def test_route_accepts_image_ref_instead_of_422():
    model = ScriptedModel(AIMessage(content="照片辨識還沒開放。"), AIMessage(content="有。"))
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: _agent(model)
    with TestClient(app) as client:
        response = client.post(
            "/agent/messages",
            json={
                "household_id": HOUSEHOLD,
                "message": "這是什麼？",
                "image_ref": "blob://households/demo-household/media/photo.jpg",
            },
        )
        assert response.status_code == 200
        without_photo = client.post(
            "/agent/messages", json={"household_id": HOUSEHOLD, "message": "還有白米嗎"}
        )
        assert without_photo.status_code == 200  # old clients keep working


# --- price search (v8 section 6, read only) ----------------------------------


def _option(option_id, price, *, source="live", store="店家"):
    return PurchaseOption(
        option_id=option_id,
        title=f"衛生紙 {option_id}",
        store=store,
        source=source,
        estimated_price=price,
        product_url=f"https://example.com/{option_id}",
        unit_spec="100抽x24包",
    )


class FakeCommerce:
    """Stands in for A2's BigGo provider and records the searches."""

    def __init__(self, options=(), error=None):
        self.options = list(options)
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search_options(self, keyword, limit=5):
        self.calls.append((keyword, limit))
        if self.error is not None:
            raise self.error
        return self.options


def _price_agent(model: ScriptedModel, commerce=None) -> ReadAgent:
    tools = QueryTools(get_inventory_service(), get_shopping_service())
    return ReadAgent(model, tools, load_system_prompt(), commerce=commerce)


def _tool_content(model: ScriptedModel) -> str:
    return [m for m in model.seen[-1] if isinstance(m, ToolMessage)][-1].content


def test_price_question_searches_and_the_program_marks_the_lowest_price():
    commerce = FakeCommerce([_option("a", 389.0), _option("b", None), _option("c", 299.0)])
    model = ScriptedModel(
        _call("search_purchase_options", item_name=" 衛生紙 "),
        AIMessage(content="目前找到的最低價格是 299 元。"),
    )

    response = _ask(_price_agent(model, commerce), "衛生紙哪裡買比較便宜？")

    assert commerce.calls == [("衛生紙", 5)]
    seen = json.loads(_tool_content(model))
    assert [o["estimated_price"] for o in seen["options"]] == [299.0, 389.0, None]
    assert [o["lowest_price_found"] for o in seen["options"]] == [True, False, False]
    (result,) = response.results
    assert result.kind is ResultKind.PURCHASE_OPTIONS
    assert result.data["demo"] is False
    assert [o["option_id"] for o in result.data["options"]] == ["c", "a", "b"]
    assert "目前找到的價格" in result.summary
    assert response.message == "目前找到的最低價格是 299 元。"


def test_demo_prices_are_labelled_as_demo():
    commerce = FakeCommerce([_option("demo-1", 389.0, source="demo")])
    model = ScriptedModel(
        _call("search_purchase_options", item_name="衛生紙"),
        AIMessage(content="這是示範資料，不是即時價格。"),
    )

    response = _ask(_price_agent(model, commerce), "衛生紙多少錢？")

    (result,) = response.results
    assert result.data["demo"] is True
    assert "示範資料" in result.summary
    assert json.loads(_tool_content(model))["options"][0]["source"] == "demo"


def test_price_search_failure_is_answered_instead_of_failing_the_request():
    commerce = FakeCommerce(error=RuntimeError("BigGo is down"))
    model = ScriptedModel(
        _call("search_purchase_options", item_name="衛生紙"),
        AIMessage(content="現在查不到價格，請稍後再試。"),
    )

    response = _ask(_price_agent(model, commerce), "衛生紙多少錢？")

    assert json.loads(_tool_content(model)) == {"error": "price_search_failed"}
    assert response.results == []
    assert response.message == "現在查不到價格，請稍後再試。"


def test_price_search_is_not_pretended_without_a_provider():
    model = ScriptedModel(
        _call("search_purchase_options", item_name="衛生紙"),
        AIMessage(content="比價還沒開放。"),
    )

    response = _ask(_price_agent(model, commerce=None), "衛生紙多少錢？")

    assert json.loads(_tool_content(model)) == {"error": "price_search_not_available"}
    assert response.results == []


def test_price_search_without_an_item_name_reads_nothing():
    commerce = FakeCommerce([_option("a", 100.0)])
    model = ScriptedModel(
        _call("search_purchase_options", item_name="  "),
        AIMessage(content="你想查哪一樣東西的價格？"),
    )

    response = _ask(_price_agent(model, commerce), "幫我比價")

    assert commerce.calls == []
    assert response.results == []


def test_no_options_found_shows_no_result_card():
    model = ScriptedModel(
        _call("search_purchase_options", item_name="火星沙"),
        AIMessage(content="目前沒有找到這樣東西的購買選項。"),
    )

    response = _ask(_price_agent(model, FakeCommerce([])), "火星沙哪裡買？")

    assert json.loads(_tool_content(model)) == {"item_name": "火星沙", "options": []}
    assert response.results == []


def test_two_searches_in_one_message_show_two_results():
    commerce = FakeCommerce([_option("a", 100.0)])
    model = ScriptedModel(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "search_purchase_options", "args": {"item_name": "衛生紙"}, "id": "c1"},
                {"name": "search_purchase_options", "args": {"item_name": "洗衣精"}, "id": "c2"},
            ],
        ),
        AIMessage(content="兩樣都查到了。"),
    )

    response = _ask(_price_agent(model, commerce), "衛生紙和洗衣精多少錢？")

    assert [r.data["item_name"] for r in response.results] == ["衛生紙", "洗衣精"]


def test_price_search_through_the_route_writes_nothing():
    commerce = FakeCommerce([_option("a", 199.0)])
    model = ScriptedModel(
        _call("search_purchase_options", item_name="衛生紙"),
        AIMessage(content="目前找到的最低價格是 199 元。"),
    )
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: _price_agent(model, commerce)
    with TestClient(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]
        response = client.post(
            "/agent/messages", json={"household_id": HOUSEHOLD, "message": "衛生紙多少錢"}
        )
        assert response.status_code == 200
        assert response.json()["results"][0]["kind"] == "purchase_options"
        assert before == [client.get(path).json() for path in ["/inventory", "/shopping"]]


def test_works_with_a2_provider_in_demo_mode():
    """A2's real provider without network: demo options, labelled as demo."""
    model = ScriptedModel(
        _call("search_purchase_options", item_name="衛生紙"),
        AIMessage(content="這是示範資料，不是即時價格。"),
    )

    response = _ask(_price_agent(model, BigGoCommerceProvider(demo_mode=True)), "衛生紙哪裡買？")

    (result,) = response.results
    assert result.data["demo"] is True
    assert 1 <= len(result.data["options"]) <= 5
    seen = json.loads(_tool_content(model))
    assert sum(o["lowest_price_found"] for o in seen["options"]) == 1


def test_prompt_keeps_the_v8_price_wording_rules():
    prompt = load_system_prompt()
    assert "目前找到的最低價格" in prompt
    assert "全網最低" in prompt  # named as forbidden
    assert "這是示範資料，不是即時價格" in prompt


# --- Day 8: reading photos -------------------------------------------------

PHOTO = "img-milk-001"


class RecordingVision(MockVisionService):
    """A2's mock vision, recording what the Agent asked for."""

    def __init__(self) -> None:
        super().__init__(get_inventory_service())
        self.calls: list[tuple[str, str, SceneType | None]] = []

    async def analyze_image(self, household_id, image_ref, scene_type=None):
        self.calls.append((household_id, image_ref, scene_type))
        return await super().analyze_image(household_id, image_ref, scene_type)


def _photo_agent(model: ScriptedModel, *, staged_for: str | None = HOUSEHOLD):
    vision = RecordingVision()
    media = LocalStagingMediaStorage()
    if staged_for is not None:
        asyncio.run(media.simulate_client_upload(staged_for, PHOTO, b"jpeg-bytes"))
    tools = QueryTools(get_inventory_service(), get_shopping_service())
    agent = ReadAgent(model, tools, load_system_prompt(), vision=vision, media=media)
    return agent, vision


def _ask_photo(agent: ReadAgent, message: str = "幫我看看這是什麼", household_id=HOUSEHOLD):
    request = AgentRequest(household_id=household_id, message=message, image_ref=PHOTO)
    return asyncio.run(agent.respond(request))


def test_photo_is_read_and_shown_without_writing():
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="看到 1 瓶高纖無糖豆漿。"))
    agent, vision = _photo_agent(model)
    response = _ask_photo(agent)

    assert vision.calls == [(HOUSEHOLD, PHOTO, None)]  # no hint: A2 decides the scene
    seen = json.loads([m for m in model.seen[-1] if isinstance(m, ToolMessage)][-1].content)
    assert seen["items"][0]["name"] == "高纖無糖豆漿"
    assert response.results[-1].kind == ResultKind.VISION
    assert response.results[-1].data["image_ref"] == PHOTO
    assert response.pending_approval is None  # display only, nothing to approve or write


def test_photo_of_another_household_is_refused_before_the_model():
    model = ScriptedModel(AIMessage(content="不應該被呼叫"))
    agent, vision = _photo_agent(model, staged_for="another-household")
    response = _ask_photo(agent)

    assert "找不到這張照片" in response.message
    assert model.seen == []  # refused before any model call
    assert vision.calls == []


def test_unknown_photo_is_refused():
    model = ScriptedModel(AIMessage(content="不應該被呼叫"))
    agent, vision = _photo_agent(model, staged_for=None)
    response = _ask_photo(agent)

    assert "找不到這張照片" in response.message
    assert vision.calls == []


def test_user_hint_is_passed_as_scene_type():
    model = ScriptedModel(
        _call("analyze_photo", scene_type="receipt"), AIMessage(content="發票上有豆漿。")
    )
    agent, vision = _photo_agent(model)
    _ask_photo(agent, "這是今天的發票")

    assert vision.calls == [(HOUSEHOLD, PHOTO, SceneType.RECEIPT)]


def test_invalid_scene_type_lets_a2_decide():
    model = ScriptedModel(
        _call("analyze_photo", scene_type="selfie"), AIMessage(content="看到豆漿。")
    )
    agent, vision = _photo_agent(model)
    _ask_photo(agent)

    assert vision.calls == [(HOUSEHOLD, PHOTO, None)]


def test_photo_ref_and_household_come_from_request_not_model():
    model = ScriptedModel(
        _call("analyze_photo", image_ref="someone-elses-photo", household_id="x"),
        AIMessage(content="看到豆漿。"),
    )
    agent, vision = _photo_agent(model)
    _ask_photo(agent)

    assert vision.calls == [(HOUSEHOLD, PHOTO, None)]


def test_analyze_photo_without_a_photo_reads_nothing():
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="這次沒有附照片。"))
    agent, vision = _photo_agent(model)
    response = _ask(agent, "幫我看照片")

    tool_message = [m for m in model.seen[-1] if isinstance(m, ToolMessage)][-1]
    assert "沒有附照片" in tool_message.content
    assert vision.calls == []
    assert response.results == []


# --- A1 matches photo items itself (Issue #36 layers 2-3, Issue #44) --------

_T = datetime(2026, 1, 1, tzinfo=UTC)


def _inventory_item(item_id: str, name: str, *aliases: str, unit: str = "瓶") -> InventoryItem:
    return InventoryItem(
        item_id=item_id,
        household_id=HOUSEHOLD,
        name=name,
        quantity=1,
        unit=unit,
        created_at=_T,
        updated_at=_T,
        aliases=list(aliases) or None,
    )


class _Inventory:
    def __init__(self, *items: InventoryItem) -> None:
        self.items = list(items)

    async def list_items(self, household_id: str):
        return list(self.items) if household_id == HOUSEHOLD else []


class _Shopping:
    async def list_items(self, household_id: str):
        return []


def _detected(name: str, **overrides) -> DetectedItem:
    values = dict(
        raw_detected_name=f"{name}（照片上的字）",
        canonical_name=name,
        quantity=1,
        unit="瓶",
        unit_confirmed=True,
        confidence=0.9,
        # A2's own guess, deliberately wrong: the Agent must not use it (Issue #44).
        matched_inventory_id="a2-guess",
        match_status=MatchStatus.EXACT_MATCH,
    )
    values.update(overrides)
    return DetectedItem(**values)


class _StubVision:
    def __init__(self, *items: DetectedItem) -> None:
        self.items = items

    async def analyze_image(self, household_id, image_ref, scene_type=None):
        return VisionAnalysisResult(
            image_ref=image_ref, scene_type=SceneType.PANTRY_SCENE, detected_items=list(self.items)
        )


def _matching_agent(model: ScriptedModel, inventory: _Inventory, *detected: DetectedItem):
    media = LocalStagingMediaStorage()
    asyncio.run(media.simulate_client_upload(HOUSEHOLD, PHOTO, b"jpeg-bytes"))
    tools = QueryTools(inventory, _Shopping())
    return ReadAgent(model, tools, load_system_prompt(), vision=_StubVision(*detected), media=media)


def _seen_by_model(model: ScriptedModel) -> dict:
    return json.loads([m for m in model.seen[-1] if isinstance(m, ToolMessage)][-1].content)


def _photo_reply(*detected: DetectedItem, inventory: _Inventory):
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="好的。"))
    agent = _matching_agent(model, inventory, *detected)
    response = _ask_photo(agent)
    return _seen_by_model(model), response


def test_photo_item_is_matched_by_a1_not_by_a2():
    soy = _inventory_item("soy", "醬油", "龜甲萬")
    seen, response = _photo_reply(_detected("龜甲萬"), inventory=_Inventory(soy))

    match = seen["items"][0]["match"]
    assert match == {"method": "alias", "item_id": "soy", "item_name": "醬油", "item_unit": "瓶"}
    assert "a2-guess" not in json.dumps(seen, ensure_ascii=False)
    assert "household_items" not in seen  # everything matched: no list for layer 3
    data = response.results[-1].data
    assert data["items"][0]["match"]["item_id"] == "soy"
    assert "a2-guess" not in json.dumps(data, ensure_ascii=False)


def test_unmatched_photo_item_hands_the_household_list_to_the_model():
    soy = _inventory_item("soy", "醬油", "龜甲萬")
    seen, response = _photo_reply(_detected("萬家香"), inventory=_Inventory(soy))

    assert seen["items"][0]["match"] == {"method": "none"}
    assert seen["household_items"] == [
        {"item_id": "soy", "name": "醬油", "aliases": ["龜甲萬"], "unit": "瓶"}
    ]
    assert "household_items" not in response.results[-1].data  # model input only
    assert response.pending_approval is None  # still display only


def test_same_name_items_are_offered_as_choices():
    a = _inventory_item("a", "金蘭醬油", "醬油")
    b = _inventory_item("b", "萬字醬油", "醬油")
    seen, _ = _photo_reply(_detected("醬油"), inventory=_Inventory(a, b))

    assert seen["items"][0]["match"] == {
        "method": "alias",
        "choices": [
            {"item_id": "a", "item_name": "金蘭醬油"},
            {"item_id": "b", "item_name": "萬字醬油"},
        ],
    }


def test_unsure_unit_and_low_confidence_are_flagged():
    seen, _ = _photo_reply(
        _detected("咖啡豆", unit=None, unit_confirmed=False, confidence=0.4),
        inventory=_Inventory(),
    )
    item = seen["items"][0]
    assert item["unit"] is None
    assert item["unit_confirmed"] is False
    assert item["low_confidence"] is True


def test_expiry_date_reaches_the_app():
    seen, response = _photo_reply(
        _detected("牛奶", expiry_date=date(2026, 11, 20)),
        _detected("白米"),
        inventory=_Inventory(),
    )

    data_items = response.results[-1].data["items"]
    assert [i["expiry_date"] for i in data_items] == ["2026-11-20", None]
    assert seen["items"][0]["expiry_date"] == "2026-11-20"


class _FailingVision:
    async def analyze_image(self, household_id, image_ref, scene_type=None):
        raise TimeoutError("vision timed out")


def test_photo_failure_is_answered_instead_of_failing_the_request(caplog):
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="照片辨識失敗了，請再拍一次。"))
    media = LocalStagingMediaStorage()
    asyncio.run(media.simulate_client_upload(HOUSEHOLD, PHOTO, b"jpeg-bytes"))
    tools = QueryTools(_Inventory(), _Shopping())
    agent = ReadAgent(model, tools, load_system_prompt(), vision=_FailingVision(), media=media)

    response = _ask_photo(agent)

    assert response.message == "照片辨識失敗了，請再拍一次。"
    assert response.results == []  # no photo card: there is nothing to show
    assert _seen_by_model(model) == {"error": "photo_analysis_failed"}
    assert "Photo analysis failed" in caplog.text


def test_several_items_are_matched_with_one_inventory_read():
    class _CountingInventory(_Inventory):
        reads = 0

        async def list_items(self, household_id: str):
            type(self).reads += 1
            return await super().list_items(household_id)

    inventory = _CountingInventory(_inventory_item("rice", "白米", unit="包"))
    seen, _ = _photo_reply(_detected("白米"), _detected("牛奶"), inventory=inventory)

    assert [i["match"]["method"] for i in seen["items"]] == ["name", "none"]
    assert _CountingInventory.reads == 1


def test_prompt_no_longer_reads_a2_match_fields():
    prompt = load_system_prompt()
    # As field names (in backticks); v8's tool name get_reorder_candidates is fine.
    for a2_field in ("match_status", "fuzzy_candidate", "candidates"):
        assert f"`{a2_field}`" not in prompt
    assert "household_items" in prompt


def test_prompt_says_not_to_retry_a_failed_photo():
    """PR #67 review: after a failed recognition, answer instead of calling the tool again."""
    assert "不要再次呼叫 `analyze_photo`" in load_system_prompt()
