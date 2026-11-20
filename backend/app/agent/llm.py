"""Vertex AI model configuration using Application Default Credentials."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_VERTEX_MODEL = "gemini-3.8-flash"
SUPPORTED_VERTEX_MODELS = (DEFAULT_VERTEX_MODEL, "gemini-3.8-flash")
DEFAULT_VERTEX_LOCATION = "us-central1"
_VERTEX_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def _validate_vertex_project(project: str) -> str:
    selected = project.strip()
    if not _VERTEX_PROJECT_ID.fullmatch(selected):
        raise ValueError(
            "Invalid Vertex project ID; set NESTOCK_VERTEX_PROJECT_ID or "
            "GOOGLE_CLOUD_PROJECT to a valid Google Cloud project ID "
            "(6-30 lowercase letters, digits, or hyphens; start with a letter)."
        )
    return selected


@dataclass(frozen=True)
class VertexAISettings:
    project: str
    location: str = DEFAULT_VERTEX_LOCATION
    model: str = DEFAULT_VERTEX_MODEL

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _validate_vertex_project(self.project))
        object.__setattr__(self, "model", _validate_vertex_model(self.model))


def _validate_vertex_model(model: str) -> str:
    selected = model.strip()
    if selected not in SUPPORTED_VERTEX_MODELS:
        allowed = ", ".join(SUPPORTED_VERTEX_MODELS)
        raise ValueError(f"Unsupported NESTOCK_VERTEX_MODEL {model!r}; expected one of: {allowed}.")
    return selected


def load_vertex_settings(
    environment: Mapping[str, str] | None = None,
) -> VertexAISettings | None:
    """Load non-secret Vertex references; authentication is delegated to ADC."""
    env = os.environ if environment is None else environment
    configured_model = env.get("NESTOCK_VERTEX_MODEL")
    model = (
        DEFAULT_VERTEX_MODEL
        if configured_model is None
        else _validate_vertex_model(configured_model)
    )
    project = env.get("NESTOCK_VERTEX_PROJECT_ID", "").strip()
    if not project:
        project = env.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        return None
    location = (
        env.get("NESTOCK_VERTEX_LOCATION") or env.get("GOOGLE_CLOUD_LOCATION") or ""
    ).strip() or DEFAULT_VERTEX_LOCATION
    return VertexAISettings(project=project, location=location, model=model)


def build_vertex_client(settings: VertexAISettings):
    """Create the Google Gen AI client in Vertex mode; credentials come from ADC."""
    from google import genai

    return genai.Client(
        vertexai=True,
        project=settings.project,
        location=settings.location,
    )


def build_chat_model(settings: VertexAISettings):
    """Create the LangChain Vertex chat model for the read-only Agent."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.model,
        project=settings.project,
        location=settings.location,
        vertexai=True,
        temperature=0,
    )
