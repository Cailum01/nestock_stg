"""A1 tool selection cases: offline structure checks plus a live selection test.

Same layout as test_b2_tool_cases.py. A case with `photo: true` gets the same
PHOTO_ATTACHED system message the Agent adds when a request carries a photo.
The live test calls the model synchronously: `asyncio.run` per case with one
shared model left its connection pool on a closed event loop, so every second
case failed with "Event loop is closed" on Windows.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.agent.llm import build_chat_model, load_azure_settings
from app.agent.read_agent import PHOTO_ATTACHED, TOOL_SCHEMAS, load_system_prompt

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "a1_tool_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
A1_TOOLS = {"list_inventory", "list_shopping", "analyze_photo", "search_purchase_options"}
TOOLS = {schema["function"]["name"]: schema["function"] for schema in TOOL_SCHEMAS}


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


@pytest.fixture(scope="module")
def live_model():
    settings = load_azure_settings()
    if settings is None:
        pytest.skip("Azure OpenAI is not configured; live tool selection not run")
    return build_chat_model(settings).bind_tools(TOOL_SCHEMAS)


@pytest.mark.live_eval
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_live_model_selects_the_expected_tools(live_model, case):
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [SystemMessage(load_system_prompt())]
    if case.get("photo"):
        messages.append(SystemMessage(PHOTO_ATTACHED))
    messages.append(HumanMessage(case["utterance"]))
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
