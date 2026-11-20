import sys
from types import SimpleNamespace

import pytest

from app.agent import read_agent
from app.agent.llm import (
    DEFAULT_VERTEX_MODEL,
    SUPPORTED_VERTEX_MODELS,
    VertexAISettings,
    build_chat_model,
    load_vertex_settings,
)
from app.agent.read_agent import get_read_agent
from app.services.commerce.biggo import BigGoCommerceProvider
from app.services.commerce.fallback import FallbackCommerceProvider
from app.services.commerce.google_shopping import GoogleShoppingCommerceProvider


def test_vertex_settings_default_model_and_reference_only_configuration():
    settings = load_vertex_settings(environment={"NESTOCK_VERTEX_PROJECT_ID": "unit-test-project"})
    assert settings == VertexAISettings(project="unit-test-project")
    assert settings is not None
    assert DEFAULT_VERTEX_MODEL == "gemini-3.8-flash"
    assert settings.model == "gemini-3.8-flash"
    assert settings.location == "us-central1"
    assert load_vertex_settings(environment={}) is None


def test_vertex_model_allowlist_has_lite_default_and_explicit_flash_fallback():
    assert SUPPORTED_VERTEX_MODELS == ("gemini-3.8-flash", "gemini-3.8-flash")
    settings = load_vertex_settings(
        environment={
            "NESTOCK_VERTEX_PROJECT_ID": "unit-test-project",
            "NESTOCK_VERTEX_MODEL": "gemini-3.8-flash",
        }
    )
    assert settings is not None
    assert settings.model == "gemini-3.8-flash"


def test_vertex_settings_accept_google_cloud_project_fallback_and_trim_values():
    settings = load_vertex_settings(environment={"GOOGLE_CLOUD_PROJECT": "  unit-test-project  "})
    assert settings == VertexAISettings(project="unit-test-project")


@pytest.mark.parametrize(
    "project",
    [" short", "Uppercase-project", "bad_project", "invalid-project-"],
)
def test_vertex_settings_reject_invalid_configured_project_ids(project):
    with pytest.raises(ValueError, match="Invalid Vertex project ID"):
        load_vertex_settings(environment={"NESTOCK_VERTEX_PROJECT_ID": project})


def test_vertex_settings_rejects_invalid_primary_project_instead_of_using_fallback():
    with pytest.raises(ValueError, match="Invalid Vertex project ID"):
        load_vertex_settings(
            environment={
                "NESTOCK_VERTEX_PROJECT_ID": "invalid_project",
                "GOOGLE_CLOUD_PROJECT": "valid-project",
            }
        )


def test_vertex_model_setting_selects_configured_fallback():
    settings = load_vertex_settings(
        environment={
            "NESTOCK_VERTEX_PROJECT_ID": "unit-test-project",
            "NESTOCK_VERTEX_MODEL": " gemini-3.8-flash ",
        }
    )
    assert settings is not None
    assert settings.model == "gemini-3.8-flash"


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "unsupported-model", " ", ""])
def test_vertex_model_setting_rejects_unsupported_values(model):
    with pytest.raises(ValueError, match="Unsupported NESTOCK_VERTEX_MODEL"):
        load_vertex_settings(
            environment={
                "NESTOCK_VERTEX_PROJECT_ID": "unit-test-project",
                "NESTOCK_VERTEX_MODEL": model,
            }
        )


def test_vertex_model_setting_is_validated_without_project_configuration():
    with pytest.raises(ValueError, match="Unsupported NESTOCK_VERTEX_MODEL"):
        load_vertex_settings(environment={"NESTOCK_VERTEX_MODEL": "unsupported-model"})


def test_agent_model_builder_selects_vertex_and_adc_without_api_key(monkeypatch):
    observed = {}

    def fake_chat_model(**kwargs):
        observed.update(kwargs)
        return "mock-chat-model"

    monkeypatch.setitem(
        sys.modules,
        "langchain_google_genai",
        SimpleNamespace(ChatGoogleGenerativeAI=fake_chat_model),
    )
    settings = load_vertex_settings(
        environment={
            "NESTOCK_VERTEX_PROJECT_ID": "unit-test-project",
            "NESTOCK_VERTEX_MODEL": "gemini-3.8-flash",
        }
    )
    assert settings is not None
    model = build_chat_model(settings)

    assert model == "mock-chat-model"
    assert observed == {
        "model": "gemini-3.8-flash",
        "project": "unit-test-project",
        "location": "us-central1",
        "vertexai": True,
        "temperature": 0,
    }


def test_read_agent_stays_unconfigured_without_vertex_project(monkeypatch):
    monkeypatch.delenv("NESTOCK_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    get_read_agent.cache_clear()
    try:
        assert get_read_agent() is None
    finally:
        get_read_agent.cache_clear()


def test_read_agent_uses_ordered_commerce_cascade_without_provider_calls(monkeypatch):
    fallback = FallbackCommerceProvider(
        primary_provider=BigGoCommerceProvider(environment={}),
        secondary_provider=GoogleShoppingCommerceProvider(environment={}),
    )

    class MockChatModel:
        def bind_tools(self, tools):
            self.tools = tools
            return self

    model = MockChatModel()
    monkeypatch.setattr(
        "app.agent.llm.load_vertex_settings",
        lambda: VertexAISettings("test-project"),
    )
    monkeypatch.setattr("app.agent.llm.build_chat_model", lambda settings: model)
    monkeypatch.setattr(read_agent, "get_commerce_service", lambda: fallback)
    get_read_agent.cache_clear()
    try:
        agent = get_read_agent()
        assert agent is not None
        assert agent._commerce is fallback
        assert isinstance(fallback.primary_provider, BigGoCommerceProvider)
        assert isinstance(fallback.secondary_provider, GoogleShoppingCommerceProvider)
    finally:
        get_read_agent.cache_clear()
