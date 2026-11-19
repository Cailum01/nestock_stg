"""A1 Azure OpenAI settings (`app/agent/llm.py`) and Agent route failures.

No network and no real `backend/.env`: the module's `.env` location is pointed at a
temporary folder, and the three settings are cleared for each test.
"""

import pytest
from fastapi.testclient import TestClient
from identity_test_support import install_scope_override

from app.agent import llm
from app.agent.llm import ENV_VARS, AzureOpenAISettings, load_azure_settings
from app.agent.read_agent import get_read_agent
from app.main import create_app

HOUSEHOLD = "demo-household"
BASE_URL = "https://nestock-demo.services.ai.azure.com/openai/v1/"


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    """Empty `.env` folder and no Azure settings in the environment."""
    monkeypatch.setattr(llm, "BACKEND_DIR", tmp_path)
    for name in ENV_VARS:
        # setenv first so the original state is restored even if load_dotenv sets it.
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    return tmp_path


def write_env(folder, **values):
    lines = [f"{name}={value}" for name, value in values.items()]
    (folder / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----- endpoint copied from Foundry ----------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://nestock-demo.services.ai.azure.com/",
        "https://nestock-demo.services.ai.azure.com",
        "nestock-demo.services.ai.azure.com",
        "https://nestock-demo.services.ai.azure.com/openai/v1/",
        "https://nestock-demo.services.ai.azure.com/openai/deployments/terra/chat/completions"
        "?api-version=2025-01-01-preview",
        "https://nestock-demo.services.ai.azure.com/api/projects/nestock",
    ],
)
def test_any_endpoint_copied_from_foundry_becomes_the_v1_base_url(endpoint):
    settings = AzureOpenAISettings(endpoint=endpoint, api_key="key", deployment="terra")

    assert settings.base_url == BASE_URL


# ----- reading the three settings -------------------------------------------------------------


def test_settings_are_read_from_backend_env(env_dir):
    write_env(
        env_dir,
        AZURE_OPENAI_ENDPOINT="https://nestock-demo.services.ai.azure.com/",
        AZURE_OPENAI_API_KEY="key-from-file",
        AZURE_OPENAI_DEPLOYMENT="terra",
    )

    settings = load_azure_settings()

    assert settings == AzureOpenAISettings(
        endpoint="https://nestock-demo.services.ai.azure.com/",
        api_key="key-from-file",
        deployment="terra",
    )


def test_spaces_around_a_setting_are_trimmed(env_dir, monkeypatch):
    """For example a value pasted with spaces into PowerShell `$env:`."""
    write_env(env_dir, AZURE_OPENAI_API_KEY="key", AZURE_OPENAI_DEPLOYMENT="terra")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "  https://nestock-demo.services.ai.azure.com/  ")

    settings = load_azure_settings()

    assert settings is not None
    assert settings.endpoint == "https://nestock-demo.services.ai.azure.com/"


@pytest.mark.parametrize("missing", ENV_VARS)
@pytest.mark.parametrize("blank", [False, True])
def test_any_missing_or_blank_setting_keeps_the_agent_off(env_dir, monkeypatch, missing, blank):
    values = {
        "AZURE_OPENAI_ENDPOINT": "https://nestock-demo.services.ai.azure.com/",
        "AZURE_OPENAI_API_KEY": "key",
        "AZURE_OPENAI_DEPLOYMENT": "terra",
    }
    del values[missing]
    write_env(env_dir, **values)
    if blank:
        monkeypatch.setenv(missing, "   ")

    assert load_azure_settings() is None


def test_environment_variables_win_over_backend_env(env_dir, monkeypatch):
    write_env(
        env_dir,
        AZURE_OPENAI_ENDPOINT="https://from-file.services.ai.azure.com/",
        AZURE_OPENAI_API_KEY="key-from-file",
        AZURE_OPENAI_DEPLOYMENT="from-file",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key-from-environment")

    settings = load_azure_settings()

    assert settings is not None
    assert settings.api_key == "key-from-environment"
    assert settings.deployment == "from-file"


def test_agent_is_not_built_without_settings(env_dir):
    get_read_agent.cache_clear()
    try:
        assert get_read_agent() is None
    finally:
        get_read_agent.cache_clear()


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
