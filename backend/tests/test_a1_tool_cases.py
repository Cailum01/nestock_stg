"""A1 tool selection cases: offline structure checks plus a live selection test.

Same layout as test_b2_tool_cases.py. A case with `photo: true` gets the same
PHOTO_ATTACHED system message the Agent adds when a request carries a photo. A
`complete_purchase` case with `shopping_list` is checked one turn later: the model
must look up the shopping list first (prompt 買回來了 rule 1), gets those rows back,
and its next turn is checked. An `add_stock` case with `inventory` works the same
way with `list_inventory` (prompt 家裡多了東西 rule 1): the model gets those rows
back the way the Agent shapes them, and its next turn is checked. Both card tools are
offered as the Agent offers them when Cosmos is configured. A case with
`stock_increase: true` (Issue #206) is asked as the Agent asks by default, with adding
to household items on: `add_stock` has that description and the model gets that system
note. A case without it is asked as with `NESTOCK_STOCK_INCREASE=0`.
The live test calls the model synchronously: `asyncio.run` per case with one
shared model left its connection pool on a closed event loop, so every second
case failed with "Event loop is closed" on Windows.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.agent.cards import PURCHASE_TOOL_SCHEMA
from app.agent.llm import build_chat_model, load_azure_settings
from app.agent.read_agent import PHOTO_ATTACHED, TOOL_SCHEMAS, load_system_prompt
from app.agent.stock_cards import INCREASE_PROMPT_NOTE, INCREASE_TOOL_SCHEMA, STOCK_TOOL_SCHEMA
from app.services.item_matching import normalize_term

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "a1_tool_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
A1_TOOLS = {
    "list_inventory",
    "list_shopping",
    "analyze_photo",
    "search_purchase_options",
    "complete_purchase",
    "add_stock",
}
OFFERED = [*TOOL_SCHEMAS, PURCHASE_TOOL_SCHEMA, STOCK_TOOL_SCHEMA]
OFFERED_INCREASING = [*TOOL_SCHEMAS, PURCHASE_TOOL_SCHEMA, INCREASE_TOOL_SCHEMA]
TOOLS = {schema["function"]["name"]: schema["function"] for schema in OFFERED}


def test_case_ids_and_utterances_are_unique():
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert len({c["utterance"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("tool", sorted(A1_TOOLS))
def test_each_a1_tool_has_three_examples_and_a_confusion_case(tool):
    kinds = Counter(c["kind"] for c in CASES if c["tool"] == tool)

    assert kinds["positive"] >= 3
    assert kinds["negative"] >= 1


def test_cases_only_name_real_tools():
    for case in CASES:
        assert case["kind"] in {"positive", "negative"}
        assert case["tool"] in A1_TOOLS
        named = set(case["expect_calls"]) | set(case["must_not_call"])
        assert named <= set(TOOLS), case["id"]


def test_positive_cases_expect_their_own_tool():
    for case in CASES:
        if case["kind"] == "positive":
            assert case["tool"] in case["expect_calls"], case["id"]
            assert case["tool"] not in case["must_not_call"], case["id"]


def test_negative_cases_do_not_expect_the_tool_they_guard():
    for case in CASES:
        if case["kind"] == "negative":
            assert case["tool"] in case["must_not_call"], case["id"]


def test_expected_arguments_exist_in_the_tool_schema():
    for case in CASES:
        for argument in case.get("args_contain", {}):
            properties = TOOLS[case["tool"]]["parameters"]["properties"]
            assert argument in properties, case["id"]


def test_photo_cases_match_the_photo_tool():
    for case in CASES:
        if case["tool"] == "analyze_photo" and case["kind"] == "positive":
            assert case.get("photo") is True, case["id"]
        if case.get("photo"):
            assert case["tool"] == "analyze_photo", case["id"]


def test_shopping_list_rows_go_with_purchase_cases():
    for case in CASES:
        rows = case.get("shopping_list")
        if case["tool"] == "complete_purchase" and case["kind"] == "positive":
            assert rows, case["id"]
        if rows is None:
            continue
        assert case["tool"] == "complete_purchase", case["id"]
        ids = {row["shopping_item_id"] for row in rows}
        assert all(row["status"] == "pending" for row in rows), case["id"]
        listed = case.get("args_contain", {}).get("items")
        assert listed is None or listed in ids, case["id"]


def test_inventory_rows_go_with_stock_cases():
    for case in CASES:
        rows = case.get("inventory")
        if case["tool"] == "add_stock" and case["kind"] == "positive":
            assert rows is not None, case["id"]
        if rows is None:
            continue
        assert case["tool"] == "add_stock", case["id"]
        assert "shopping_list" not in case, case["id"]
        assert all({"item_id", "name", "unit"} <= row.keys() for row in rows), case["id"]


def test_the_increase_switch_goes_with_stock_cases_and_has_its_own_examples():
    switched = [case for case in CASES if "stock_increase" in case]
    assert all(case["stock_increase"] is True for case in switched)
    assert all(case["tool"] == "add_stock" for case in switched)
    kinds = Counter(case["kind"] for case in switched)
    assert kinds["positive"] >= 3 and kinds["negative"] >= 1
    household = [
        case
        for case in switched
        if case["kind"] == "positive"
        and any(row["name"] == case["args_contain"]["items"] for row in case["inventory"])
    ]
    assert len(household) >= 3  # ones the household has, which is what the switch adds


def test_the_tool_differs_only_in_its_description_when_increasing():
    plain, increasing = STOCK_TOOL_SCHEMA["function"], INCREASE_TOOL_SCHEMA["function"]
    assert increasing["name"] == plain["name"] == "add_stock"
    assert increasing["parameters"] == plain["parameters"]
    assert increasing["description"] != plain["description"]


def _inventory_answer(rows, args):
    """`list_inventory` as `ReadAgent._inventory_answer` shapes it for the model."""
    name = str(args.get("item_name") or "").strip()
    if not name:
        return {"inventory": rows}
    key = normalize_term(name)
    found = [
        row
        for row in rows
        if normalize_term(row["name"]) == key
        or any(normalize_term(alias) == key for alias in row.get("aliases", []))
    ]
    if found:
        return {"item_name": name, "inventory": found}
    household = [
        {
            k: row.get(k, []) if k == "aliases" else row[k]
            for k in ("item_id", "name", "aliases", "unit")
        }
        for row in rows
    ]
    return {"item_name": name, "found": False, "household_items": household}


def _looked_up(case, call):
    """What the model gets back from a lookup in a case's first turn."""
    if call["name"] == "list_shopping" and "shopping_list" in case:
        return case["shopping_list"]
    if call["name"] == "list_inventory" and "inventory" in case:
        return _inventory_answer(case["inventory"], call.get("args") or {})
    return []


@pytest.fixture(scope="module")
def live_models():
    settings = load_azure_settings()
    if settings is None:
        pytest.skip("Azure OpenAI is not configured; live tool selection not run")
    model = build_chat_model(settings)
    return {False: model.bind_tools(OFFERED), True: model.bind_tools(OFFERED_INCREASING)}


@pytest.mark.live_eval
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_live_model_selects_the_expected_tools(live_models, case):
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    increasing = bool(case.get("stock_increase"))
    live_model = live_models[increasing]
    messages = [SystemMessage(load_system_prompt())]
    if increasing:
        messages.append(SystemMessage(INCREASE_PROMPT_NOTE))  # as `ReadAgent.respond` does
    if case.get("photo"):
        messages.append(SystemMessage(PHOTO_ATTACHED))
    messages.append(HumanMessage(case["utterance"]))
    reply = live_model.invoke(messages)
    lookup = (
        "list_shopping"
        if "shopping_list" in case
        else "list_inventory"
        if "inventory" in case
        else None
    )
    if lookup is not None:
        first = {call["name"] for call in reply.tool_calls}
        assert lookup in first, (case["id"], "looks it up first", first)
        messages.append(reply)
        for call in reply.tool_calls:
            content = json.dumps(_looked_up(case, call), ensure_ascii=False)
            messages.append(ToolMessage(content, tool_call_id=call["id"]))
        reply = live_model.invoke(messages)
    calls = reply.tool_calls
    names = {call["name"] for call in calls}

    assert set(case["expect_calls"]) <= names, (case["id"], names)
    assert not names & set(case["must_not_call"]), (case["id"], names)
    if not case["expect_calls"]:
        assert not calls, (case["id"], names)
    for argument, text in case.get("args_contain", {}).items():
        called = next(c for c in calls if c["name"] == case["tool"])
        assert text in str(called["args"].get(argument, "")), (case["id"], called["args"])
