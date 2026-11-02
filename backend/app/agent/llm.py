"""Azure OpenAI connection for the Agent (A1).

Uses the Azure OpenAI v1 API (`https://<resource>/openai/v1/`, no api-version) with
the Responses API, as shown on the Foundry deployment page.
Settings come from environment variables, optionally loaded from backend/.env.
Never commit .env; backend/.env.example lists the variable names only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]

ENV_VARS = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
)


@dataclass(frozen=True)
class AzureOpenAISettings:
    endpoint: str
    api_key: str
    deployment: str

    @property
    def base_url(self) -> str:
        """Any endpoint copied from Foundry -> https://<host>/openai/v1/"""
        parts = urlsplit(self.endpoint if "://" in self.endpoint else f"https://{self.endpoint}")
        return f"{parts.scheme}://{parts.netloc}/openai/v1/"


def load_azure_settings() -> AzureOpenAISettings | None:
    """Return settings, or None when any variable is missing (Agent stays disabled)."""
    load_dotenv(BACKEND_DIR / ".env", override=False)
    values = [os.environ.get(name, "").strip() for name in ENV_VARS]
    if not all(values):
        return None
    endpoint, api_key, deployment = values
    return AzureOpenAISettings(endpoint, api_key, deployment)


def build_chat_model(settings: AzureOpenAISettings):
    """LangChain chat model for LangGraph; imported lazily so tests never need it."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.deployment,
        use_responses_api=True,
        timeout=60,
        max_retries=2,
    )
