"""A1-50: a purchase day the user says (「昨天買的」) on the purchase card.

Built but off in the running app unless `NESTOCK_DATED_PURCHASES=1` (local testing):
the App shows only name, quantity and unit on a purchase card, and a card may only hold values
the user sees (HITL rule 4). Contract section 11. Same faked Cosmos container and real
services as `test_purchase_cards`; the clock is T0 = 2026-11-10 16:00 Taipei.
"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_purchase_cards import (
    HOME,
    T0,
    ScriptedModel,
    ask,
    inventory,
    purchase_reply,
    resume_app,
    setup,
    shopping,
    tool_results,
)

from app.agent import read_agent
from app.agent.cards import (
    DATED_PURCHASE_TOOL_SCHEMA,
    FUTURE_DAY,
    PURCHASE_TOOL_SCHEMA,
    UNREADABLE_DAY,
    PurchaseCards,
)
from app.agent.llm import build_chat_model, load_azure_settings
from app.agent.read_agent import (
    DATED_PURCHASES_VAR,
    TOOL_SCHEMAS,
    ReadAgent,
    _text,
    dated_purchases_on,
    load_system_prompt,
)
from app.services.cosmos_read import CosmosInventoryService, CosmosShoppingService
from app.tools.queries import QueryTools

YESTERDAY_NOON = "2026-11-09T12:00:00+08:00"


def dated_agent(model, reader, approvals, dated=True):
    cards = PurchaseCards(reader, approvals, dated_purchases=dated, clock=lambda: T0)
    tools = QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader))
    return ReadAgent(model, tools, load_system_prompt(), purchases=cards)


def card_for(reader, approvals, item, dated=True, message="衛生紙昨天買的，2 包"):
    model = ScriptedModel(
        purchase_reply(item, call_id="call-1"), AIMessage(content="請確認下方的卡片。")
    )
    return ask(dated_agent(model, reader, approvals, dated), message), model


def offered_item_fields(model):
    (purchase,) = [t for t in model.bound_tools if t["function"]["name"] == "complete_purchase"]
    return purchase["function"]["parameters"]["properties"]["items"]["items"]["properties"]


def notes(model):
    return [m.content for m in model.seen[0] if isinstance(m, SystemMessage)]


def test_off_the_model_gets_no_day_field_and_no_date():
    _, reader, approvals = setup()

    response, model = card_for(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": "2026-11-09"},
        dated=False,
    )

    assert "purchased_on" not in offered_item_fields(model)
    assert PurchaseCards(reader, approvals).tool_schema is PURCHASE_TOOL_SCHEMA
    assert not any("今天是" in note for note in notes(model))
    (line,) = response.pending_approval.operations
    assert "purchased_at" not in line.values  # a day sent anyway is ignored
    assert line.description == "衛生紙 +2 包（從待購清單完成購買）"


def test_on_the_model_gets_the_day_field_and_today_in_taipei():
    _, reader, approvals = setup()

    _, model = card_for(reader, approvals, {"shopping_item_id": "tissue-1", "quantity": 2})

    assert "purchased_on" in offered_item_fields(model)
    assert any("今天是 2026-11-10（台北時間）" in note for note in notes(model))


def test_yesterday_goes_on_the_line_as_noon_taipei_and_in_its_text():
    container, reader, approvals = setup()

    response, model = card_for(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": "2026-11-09"},
    )

    (line,) = response.pending_approval.operations
    assert line.values["purchased_at"] == YESTERDAY_NOON
    assert line.description == "衛生紙 +2 包（11/9 買的，從待購清單完成購買）"
    assert json.loads(tool_results(model)[-1])["card"]["lines"] == [line.description]
    assert container.of_type("transaction") == []  # still only a pending card


@pytest.mark.parametrize("day", ["2026-11-10", "", None])
def test_today_or_no_day_uses_the_confirmation_time(day):
    _, reader, approvals = setup()
    item = {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": day}

    response, _ = card_for(reader, approvals, {k: v for k, v in item.items() if v is not None})

    (line,) = response.pending_approval.operations
    assert "purchased_at" not in line.values
    assert line.description == "衛生紙 +2 包（從待購清單完成購買）"


@pytest.mark.parametrize(("day", "reason"), [("2026-11-11", FUTURE_DAY), ("昨天", UNREADABLE_DAY)])
def test_a_future_or_unreadable_day_leaves_the_item_off(day, reason):
    container, reader, approvals = setup()

    response, model = card_for(
        reader, approvals, {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": day}
    )

    assert response.pending_approval is None
    assert json.loads(tool_results(model)[-1])["not_on_card"] == [
        {"shopping_item_id": "tissue-1", "reason": reason}
    ]
    assert container.of_type("pending_approval") == []


def test_a_new_item_line_carries_the_day_too():
    _, reader, approvals = setup(inventory(), shopping("paste-1", "牙膏", None, 1, "條"))

    response, _ = card_for(
        reader,
        approvals,
        {
            "shopping_item_id": "paste-1",
            "quantity": 1,
            "location": "浴室櫃子",
            "purchased_on": "2026-11-09",
        },
        message="牙膏昨天買的，1 條，放浴室櫃子",
    )

    (line,) = response.pending_approval.operations
    assert line.values["purchased_at"] == YESTERDAY_NOON
    assert line.description == (
        "會新增一個庫存品項：牙膏 1 條，放在浴室櫃子（11/9 買的，從待購清單完成購買）"
    )


def test_confirming_a_dated_card_dates_the_transaction_and_event():
    container, reader, approvals = setup()
    response, _ = card_for(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2, "purchased_on": "2026-11-09"},
    )
    card = response.pending_approval
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": True}

    with TestClient(resume_app(approvals)) as client:
        first = client.post("/agent/resume", json=body)
        again = client.post("/agent/resume", json=body)  # a retry keeps the same time

    assert first.status_code == 200
    assert again.json()["message"] == first.json()["message"]
    (transaction,) = container.of_type("transaction")
    (event,) = container.of_type("change_event")
    noon = datetime.fromisoformat(YESTERDAY_NOON)
    assert datetime.fromisoformat(transaction["created_at"]) == noon
    assert datetime.fromisoformat(event["occurred_at"]) == noon
    assert datetime.fromisoformat(event["recorded_at"]) == T0  # when the user confirmed
    assert container.get("inventory_item:tissue")["quantity"] == 3


@pytest.mark.parametrize(
    ("value", "on"),
    [("1", True), (" 1 ", True), ("0", False), ("true", False), ("", False), (None, False)],
)
def test_the_switch_is_on_only_when_the_variable_is_1(value, on):
    environment = {} if value is None else {DATED_PURCHASES_VAR: value}

    assert dated_purchases_on(environment) is on


@pytest.mark.parametrize(("value", "schema"), [("1", "dated"), (None, "plain")])
def test_the_running_app_reads_the_switch(monkeypatch, value, schema):
    _, reader, approvals = setup()
    monkeypatch.setattr(read_agent, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(read_agent, "get_cosmos_read_repository", lambda: reader)
    if value is None:
        monkeypatch.delenv(DATED_PURCHASES_VAR, raising=False)
    else:
        monkeypatch.setenv(DATED_PURCHASES_VAR, value)

    cards = read_agent._purchase_cards()

    expected = DATED_PURCHASE_TOOL_SCHEMA if schema == "dated" else PURCHASE_TOOL_SCHEMA
    assert cards is not None and cards.tool_schema is expected


# ----- live model (run with -m live_eval, Issue #127) ----------------------------------
# Contract section 11: dated purchases stay off in the cloud until these pass.

LIVE_TODAY = "2026-11-10"  # T0 in Taipei, as the date note tells the model
LIVE_ROWS = {
    "list_shopping": [
        {
            "shopping_item_id": "tissue-1",
            "name": "衛生紙",
            "quantity": 2,
            "unit": "包",
            "status": "pending",
            "item_id": "tissue",
        },
        {
            "shopping_item_id": "detergent-1",
            "name": "洗衣精",
            "quantity": 2,
            "unit": "瓶",
            "status": "pending",
            "item_id": "detergent",
        },
    ],
    "list_inventory": [
        {"item_id": "tissue", "name": "衛生紙", "quantity": 8, "unit": "包"},
        {"item_id": "detergent", "name": "洗衣精", "quantity": 1, "unit": "瓶"},
    ],
}
# (id, what the user says, the day each line should get; None: no day on the card)
LIVE_DAYS = [
    ("yesterday", "衛生紙昨天買回來了，買了 2 包", {"tissue-1": "2026-11-09"}),
    ("day-before-yesterday", "洗衣精前天買了 2 瓶", {"detergent-1": "2026-11-08"}),
    ("month-day", "衛生紙是 11/7 買的，2 包", {"tissue-1": "2026-11-07"}),
    (
        "two-days",
        "衛生紙昨天買了 2 包，洗衣精今天買了 2 瓶",
        {"tissue-1": "2026-11-09", "detergent-1": None},
    ),
    ("no-day", "衛生紙買回來了，2 包", {"tissue-1": None}),
    ("today", "洗衣精今天買了 2 瓶", {"detergent-1": None}),
]


def test_live_day_cases_are_past_days_on_listed_lines():
    listed = {row["shopping_item_id"] for row in LIVE_ROWS["list_shopping"]}
    assert len({case[0] for case in LIVE_DAYS}) == len(LIVE_DAYS)
    for case_id, _, days in LIVE_DAYS:
        assert set(days) <= listed, case_id
        for day in days.values():
            assert day is None or day < LIVE_TODAY, case_id


@pytest.fixture(scope="module")
def live_dated_model():
    settings = load_azure_settings()
    if settings is None:
        pytest.skip("Azure OpenAI is not configured; live check not run")
    return build_chat_model(settings).bind_tools([*TOOL_SCHEMAS, DATED_PURCHASE_TOOL_SCHEMA])


@pytest.mark.live_eval
@pytest.mark.parametrize(
    "utterance, days", [case[1:] for case in LIVE_DAYS], ids=[case[0] for case in LIVE_DAYS]
)
def test_live_model_turns_the_spoken_day_into_purchased_on(live_dated_model, utterance, days):
    """The messages the Agent sends with dated purchases on; the card's day is checked.

    No day may come back missing, empty or as today: the card then carries no day.
    """
    _, reader, approvals = setup()
    note = PurchaseCards(reader, approvals, dated_purchases=True, clock=lambda: T0).date_note()
    assert LIVE_TODAY in note
    messages = [SystemMessage(load_system_prompt()), SystemMessage(note), HumanMessage(utterance)]
    purchase = None
    for _ in range(3):
        reply = live_dated_model.invoke(messages)
        purchase = next((c for c in reply.tool_calls if c["name"] == "complete_purchase"), None)
        if purchase is not None or not reply.tool_calls:
            break
        messages.append(reply)
        for call in reply.tool_calls:
            rows = LIVE_ROWS.get(call["name"], [])
            content = json.dumps(rows, ensure_ascii=False)
            messages.append(ToolMessage(content, tool_call_id=call["id"]))

    assert purchase is not None, (utterance, _text(reply))
    items = purchase["args"]["items"]
    said = {item["shopping_item_id"]: item.get("purchased_on") for item in items}
    assert set(said) == set(days), (utterance, purchase["args"])
    for line, day in days.items():
        allowed = {day} if day else {None, "", LIVE_TODAY}
        assert said[line] in allowed, (utterance, line, said[line])
