"""B2 tool selection cases: offline structure checks plus an opt-in live selection test."""

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.llm import build_chat_model, load_azure_settings
from app.agent.read_agent import TOOL_SCHEMAS, load_system_prompt

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "b2_tool_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
B2_TOOLS = {"get_reorder_candidates", "get_consumption_forecast"}
TOOLS = {schema["function"]["name"]: schema["function"] for schema in TOOL_SCHEMAS}


def test_case_ids_and_utterances_are_unique():
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert len({c["utterance"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("tool", sorted(B2_TOOLS))
def test_each_b2_tool_has_three_examples_and_a_confusion_case(tool):
    kinds = Counter(c["kind"] for c in CASES if c["tool"] == tool)

    assert kinds["positive"] >= 3
    assert kinds["negative"] >= 1


def test_cases_only_name_real_tools():
    for case in CASES:
        assert case["kind"] in {"positive", "negative"}
        assert case["tool"] in B2_TOOLS
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


def test_b2_tool_descriptions_say_when_to_call_and_that_they_do_not_write():
    for name in B2_TOOLS:
        description = TOOLS[name]["description"]
        assert "不會改庫存" in description
        assert "才呼叫" in description


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

    reply = live_model.invoke(
        [SystemMessage(load_system_prompt()), HumanMessage(case["utterance"])]
    )
    calls = reply.tool_calls
    names = {call["name"] for call in calls}

    assert set(case["expect_calls"]) <= names, (case["id"], names)
    assert not names & set(case["must_not_call"]), (case["id"], names)
    if not case["expect_calls"]:
        assert not calls, (case["id"], names)
    for argument, text in case.get("args_contain", {}).items():
        called = next(c for c in calls if c["name"] == case["tool"])
        assert text in str(called["args"].get(argument, "")), (case["id"], called["args"])


def test_no_tool_case_rejects_an_unexpected_unlisted_tool():
    case = next(case for case in CASES if not case["expect_calls"])
    reply = SimpleNamespace(tool_calls=[{"name": "list_shopping", "args": {}}])
    model = SimpleNamespace(invoke=lambda messages: reply)

    with pytest.raises(AssertionError):
        test_live_model_selects_the_expected_tools(model, case)
