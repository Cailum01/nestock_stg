"""A1 read-only Agent: B2 forecasts ("what is running low", "how long will X last").

A scripted fake model; no network, no Azure cost. B2's real ForecastService runs on
the demo inventory (白米) and demo purchase history with a fixed clock.
"""

import asyncio
import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from identity_test_support import install_scope_override
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.read_agent import ReadAgent, get_read_agent, load_system_prompt
from app.core.dependencies import get_inventory_service, get_shopping_service
from app.main import create_app
from app.models.agent import AgentRequest, ResultKind
from app.models.domain import InventoryItem
from app.models.forecast import ForecastStatus, ItemForecast, SuggestedAction
from app.services.forecast import ForecastService, ForecastSettings, reorder_summary
from app.services.transaction_history import DemoTransactionHistory
from app.tools.queries import QueryTools

HOUSEHOLD = "demo-household"
NOW = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)


class ScriptedModel:
    """Stands in for Azure OpenAI: returns prepared replies and records what it saw."""

    def __init__(self, *replies: AIMessage) -> None:
        self.replies = list(replies)
        self.seen: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def _call(name: str, call_id: str = "call-1", **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _seen(model: ScriptedModel):
    tool_messages = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    content = tool_messages[-1].content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


def _real_forecast(*, switch_on: bool) -> ForecastService:
    """B2's service on the demo data. `switch_on` = B2's `time_semantics_confirmed` setting."""
    return ForecastService(
        get_inventory_service(),
        DemoTransactionHistory(clock=lambda: NOW),
        ForecastSettings(time_semantics_confirmed=switch_on),
    )


def _item(item_id: str, name: str, quantity: float = 1, unit: str = "包") -> InventoryItem:
    return InventoryItem(
        item_id=item_id,
        household_id=HOUSEHOLD,
        name=name,
        quantity=quantity,
        unit=unit,
        created_at=NOW,
        updated_at=NOW,
    )


def _forecast(item: InventoryItem, *, days: float | None, restock: bool = False) -> ItemForecast:
    return ItemForecast(
        household_id=HOUSEHOLD,
        item_id=item.item_id,
        status=ForecastStatus.ESTIMATED if days is not None else ForecastStatus.INSUFFICIENT_DATA,
        suggested_action=SuggestedAction.CONSIDER_RESTOCK if restock else SuggestedAction.NONE,
        confirmed_quantity=item.quantity,
        unit=item.unit,
        generated_at=NOW,
        consumption_event_count=0,
        purchase_event_count=3,
        days_until_empty=days,
    )


class _Inventory:
    def __init__(self, *items: InventoryItem) -> None:
        self.items = list(items)

    async def list_items(self, household_id: str):
        return [i for i in self.items if i.household_id == household_id]


class FakeForecast:
    """B2's forecast with prepared results; records which household was asked."""

    def __init__(self, forecasts=(), error=None) -> None:
        self.forecasts = list(forecasts)
        self.error = error
        self.calls: list[tuple[str, datetime]] = []

    async def forecast_household(self, household_id: str, now: datetime):
        self.calls.append((household_id, now))
        if self.error is not None:
            raise self.error
        return self.forecasts

    async def reorder_candidates(self, household_id: str, now: datetime):
        """B2's own picking and ordering, run on the prepared results."""
        return reorder_summary(await self.forecast_household(household_id, now))


def _agent(model, forecast, inventory=None) -> ReadAgent:
    tools = QueryTools(inventory or get_inventory_service(), get_shopping_service())
    return ReadAgent(model, tools, load_system_prompt(), forecast=forecast, clock=lambda: NOW)


def _ask(agent: ReadAgent, message: str, household_id: str = HOUSEHOLD):
    return asyncio.run(agent.respond(AgentRequest(household_id=household_id, message=message)))


# ----- "what is running low" ----------------------------------------------------------------


def test_running_low_lists_only_flagged_items_soonest_first():
    tissue, soap, rice, salt = (
        _item("tissue", "衛生紙"),
        _item("soap", "肥皂"),
        _item("rice", "白米"),
        _item("salt", "鹽"),
    )
    forecast = FakeForecast(
        [
            _forecast(tissue, days=4.6, restock=True),
            _forecast(soap, days=1.2, restock=True),
            _forecast(rice, days=45.0),
            _forecast(salt, days=None),
        ]
    )
    model = ScriptedModel(
        _call("get_reorder_candidates"), AIMessage(content="肥皂和衛生紙快用完了（預估）。")
    )

    response = _ask(
        _agent(model, forecast, _Inventory(tissue, soap, rice, salt)), "家裡有什麼快用完了？"
    )

    seen = _seen(model)
    assert [(row["name"], row["days_left"]) for row in seen["running_low"]] == [
        ("肥皂", 1),
        ("衛生紙", 5),
    ]
    assert (seen["estimated_count"], seen["insufficient_data_count"]) == (3, 1)
    (result,) = response.results
    assert result.kind is ResultKind.FORECAST
    assert "預估" in result.summary
    assert forecast.calls == [(HOUSEHOLD, NOW)]


def test_running_low_leaves_out_items_no_longer_in_the_household_list():
    tissue, soap = _item("tissue", "衛生紙"), _item("soap", "肥皂")
    forecast = FakeForecast(
        [_forecast(tissue, days=4.0, restock=True), _forecast(soap, days=1.0, restock=True)]
    )
    model = ScriptedModel(_call("get_reorder_candidates"), AIMessage(content="衛生紙快用完了。"))

    _ask(_agent(model, forecast, _Inventory(tissue)), "什麼快用完了？")

    assert [row["name"] for row in _seen(model)["running_low"]] == ["衛生紙"]


def test_running_low_with_the_switch_off_says_there_is_not_enough_data():
    model = ScriptedModel(
        _call("get_reorder_candidates"), AIMessage(content="紀錄還不夠，目前沒辦法判斷。")
    )

    response = _ask(_agent(model, _real_forecast(switch_on=False)), "什麼快用完了？")

    assert _seen(model) == {"running_low": [], "estimated_count": 0, "insufficient_data_count": 1}
    assert response.results == []


def test_running_low_with_the_switch_on_and_demo_data_finds_nothing_low():
    model = ScriptedModel(
        _call("get_reorder_candidates"), AIMessage(content="目前沒有快用完的東西。")
    )

    _ask(_agent(model, _real_forecast(switch_on=True)), "什麼快用完了？")

    assert _seen(model) == {"running_low": [], "estimated_count": 1, "insufficient_data_count": 0}


# ----- "how long will X last" --------------------------------------------------------------


def test_one_item_forecast_uses_b2_and_the_programs_rounded_days():
    model = ScriptedModel(
        _call("get_consumption_forecast", item_name="白米"),
        AIMessage(content="白米預估大約還能用 45 天。"),
    )

    response = _ask(_agent(model, _real_forecast(switch_on=True)), "白米還能用多久？")

    seen = _seen(model)
    assert (seen["name"], seen["status"], seen["days_left"]) == ("白米", "estimated", 45)
    (result,) = response.results
    assert result.kind is ResultKind.FORECAST
    assert "45 天" in result.summary


def test_one_item_without_enough_data_shows_no_card():
    model = ScriptedModel(
        _call("get_consumption_forecast", item_name="白米"),
        AIMessage(content="白米的紀錄還不夠，沒辦法預估。"),
    )

    response = _ask(_agent(model, _real_forecast(switch_on=False)), "白米還能用多久？")

    assert _seen(model)["status"] == "insufficient_data"
    assert _seen(model)["days_left"] is None
    assert response.results == []


def test_unknown_item_gives_the_household_items_to_compare():
    model = ScriptedModel(
        _call("get_consumption_forecast", item_name="火星沙"),
        AIMessage(content="庫存裡沒有火星沙。"),
    )

    _ask(_agent(model, _real_forecast(switch_on=True)), "火星沙還能用多久？")

    seen = _seen(model)
    assert seen["found"] is False
    assert [i["name"] for i in seen["household_items"]] == ["白米"]


def test_two_items_with_the_same_name_are_listed_as_choices():
    first, second = _item("milk-1", "牛奶", unit="瓶"), _item("milk-2", "牛奶", unit="瓶")
    forecast = FakeForecast([_forecast(first, days=3.0), _forecast(second, days=9.0)])
    model = ScriptedModel(
        _call("get_consumption_forecast", item_name="牛奶"),
        AIMessage(content="家裡有兩個牛奶，是哪一個？"),
    )

    response = _ask(_agent(model, forecast, _Inventory(first, second)), "牛奶還能用多久？")

    assert [c["item_id"] for c in _seen(model)["choices"]] == ["milk-1", "milk-2"]
    assert response.results == []


def test_item_forecast_without_a_name_reads_nothing():
    forecast = FakeForecast([])
    model = ScriptedModel(
        _call("get_consumption_forecast", item_name="  "),
        AIMessage(content="你想問哪一樣東西？"),
    )

    _ask(_agent(model, forecast), "還能用多久？")

    assert _seen(model) == "沒有指定要預估的品名。"


# ----- failures, scope and writes -------------------------------------------------------------


def test_forecast_failure_is_answered_instead_of_failing_the_request():
    model = ScriptedModel(
        _call("get_reorder_candidates"), AIMessage(content="現在查不到預估，請稍後再試。")
    )

    response = _ask(
        _agent(model, FakeForecast(error=RuntimeError("Cosmos timeout"))), "什麼快用完了？"
    )

    assert _seen(model) == {"error": "forecast_failed"}
    assert response.results == []
    assert response.message == "現在查不到預估，請稍後再試。"


def test_forecast_is_not_pretended_without_the_service():
    model = ScriptedModel(_call("get_reorder_candidates"), AIMessage(content="預估還沒開放。"))

    _ask(_agent(model, forecast=None), "什麼快用完了？")

    assert _seen(model) == {"error": "forecast_not_available"}


def test_household_comes_from_the_request_not_the_model():
    forecast = FakeForecast([])
    model = ScriptedModel(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_reorder_candidates",
                    "args": {"household_id": "someone-else"},
                    "id": "c1",
                }
            ],
        ),
        AIMessage(content="目前沒有快用完的東西。"),
    )

    _ask(_agent(model, forecast), "什麼快用完了？", household_id="home-2")

    assert forecast.calls == [("home-2", NOW)]


def test_forecast_through_the_route_writes_nothing():
    model = ScriptedModel(
        _call("get_consumption_forecast", item_name="白米"),
        AIMessage(content="白米預估大約還能用 45 天。"),
    )
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: _agent(model, _real_forecast(switch_on=True))
    install_scope_override(app, HOUSEHOLD)
    with TestClient(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]
        response = client.post(
            "/agent/messages", json={"household_id": HOUSEHOLD, "message": "白米還能用多久"}
        )
        assert response.status_code == 200
        assert response.json()["results"][0]["kind"] == "forecast"
        assert before == [client.get(path).json() for path in ["/inventory", "/shopping"]]


def test_prompt_keeps_forecasts_out_of_plain_stock_questions():
    prompt = load_system_prompt()
    assert "只有使用者明確問這兩種問題才查預估" in prompt
    assert "`days_left` 是程式算好的天數" in prompt
