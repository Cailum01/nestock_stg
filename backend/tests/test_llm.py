"""Vertex Agent configuration and route failures; no credentials or provider calls."""

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from identity_test_support import install_scope_override

from app.agent import llm
from app.agent.read_agent import get_read_agent
from app.main import create_app

HOUSEHOLD = "demo-household"


def test_vertex_settings_use_project_references_and_fixed_model():
    settings = llm.load_vertex_settings(
        environment={
            "NESTOCK_VERTEX_PROJECT_ID": "unit-test-project",
            "NESTOCK_VERTEX_LOCATION": "asia-east1",
        }
    )
    assert settings is not None
    assert settings.project == "unit-test-project"
    assert settings.location == "asia-east1"
    assert settings.model == "gemini-3.8-flash"
    assert llm.load_vertex_settings(environment={}) is None


def test_vertex_model_builder_uses_adc_and_fixed_model(monkeypatch):
    observed = {}

    def fake_chat_model(**kwargs):
        observed.update(kwargs)
        return "mock-chat-model"

    monkeypatch.setitem(
        sys.modules,
        "langchain_google_genai",
        SimpleNamespace(ChatGoogleGenerativeAI=fake_chat_model),
    )
    settings = llm.VertexAISettings(project="unit-test-project")
    assert llm.build_chat_model(settings) == "mock-chat-model"
    assert observed["vertexai"] is True
    assert observed["model"] == "gemini-3.8-flash"
    assert observed["project"] == "unit-test-project"


def test_vertex_client_uses_project_location_and_adc_mode(monkeypatch):
    import google.genai

    observed = {}

    def fake_client(**kwargs):
        observed.update(kwargs)
        return "mock-genai-client"

    monkeypatch.setattr(google.genai, "Client", fake_client)
    assert (
        llm.build_vertex_client(llm.VertexAISettings(project="unit-test-project"))
        == "mock-genai-client"
    )
    assert observed == {
        "vertexai": True,
        "project": "unit-test-project",
        "location": "us-central1",
    }


def test_agent_is_not_built_without_project(monkeypatch):
    monkeypatch.delenv("NESTOCK_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    get_read_agent.cache_clear()
    try:
        assert get_read_agent() is None
    finally:
        get_read_agent.cache_clear()


def test_agent_route_keeps_501_when_vertex_is_unconfigured():
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: None
    install_scope_override(app, HOUSEHOLD)
    with TestClient(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]
        response = client.post(
            "/agent/messages", json={"household_id": HOUSEHOLD, "message": "還有白米嗎"}
        )

        assert response.status_code == 501
        assert before == [client.get(path).json() for path in ["/inventory", "/shopping"]]


# ----- route failures --------------------------------------------------------------------------


class FailingAgent:
    def __init__(self, error):
        self.error = error

    async def respond(self, request):
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("upstream said: api-key=secret-value"),
        TimeoutError("read timed out after 60 s"),
    ],
)
def test_model_failure_answers_502_without_details_or_writes(error):
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: FailingAgent(error)
    install_scope_override(app, HOUSEHOLD)
    with TestClient(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]

        response = client.post(
            "/agent/messages", json={"household_id": HOUSEHOLD, "message": "還有白米嗎"}
        )

        assert response.status_code == 502
        assert response.json() == {"detail": f"Agent call failed: {type(error).__name__}"}
        assert str(error) not in response.text
        assert before == [client.get(path).json() for path in ["/inventory", "/shopping"]]
