"""A1 read-only Agent tests (Day 5). A scripted fake model; no network, no Azure cost."""

import asyncio
import json
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.read_agent import ReadAgent, get_read_agent, load_system_prompt
from app.core.dependencies import get_inventory_service, get_shopping_service
from app.main import create_app
from app.models.agent import AgentRequest, ResultKind
from app.models.domain import InventoryItem
from app.models.vision import DetectedItem, MatchStatus, SceneType, VisionAnalysisResult
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
    assert names == {"list_inventory", "list_shopping", "analyze_photo"}


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
    for a2_field in ("match_status", "fuzzy_candidate", "candidates"):
        assert a2_field not in prompt
    assert "household_items" in prompt


def test_prompt_says_not_to_retry_a_failed_photo():
    """PR #67 review: after a failed recognition, answer instead of calling the tool again."""
    assert "不要再次呼叫 `analyze_photo`" in load_system_prompt()
