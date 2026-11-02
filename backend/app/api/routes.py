from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException

from app.agent.read_agent import ReadAgent, get_read_agent
from app.core.config import settings
from app.core.dependencies import get_inventory_service, get_shopping_service
from app.models.agent import AgentRequest, AgentResponse, ResumeRequest
from app.models.base import ContractModel
from app.models.domain import InventoryItem, ShoppingItem
from app.services.interfaces import InventoryService, ShoppingService

router = APIRouter()


class HealthResponse(ContractModel):
    status: Literal["ok"] = "ok"


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/inventory", response_model=list[InventoryItem], tags=["demo"])
async def inventory(
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> list[InventoryItem]:
    return await service.list_items(settings.demo_household_id)


@router.get("/shopping", response_model=list[ShoppingItem], tags=["demo"])
async def shopping(
    service: Annotated[ShoppingService, Depends(get_shopping_service)],
) -> list[ShoppingItem]:
    return await service.list_items(settings.demo_household_id)


@router.post(
    "/agent/messages",
    response_model=AgentResponse,
    tags=["provisional"],
    responses={
        501: {"description": "Azure OpenAI not configured (backend/.env)"},
        502: {"description": "Azure OpenAI call failed"},
    },
)
async def agent_message(
    request: AgentRequest,
    agent: Annotated[ReadAgent | None, Depends(get_read_agent)],
) -> AgentResponse:
    """Read-only Agent (Day 5): answers inventory/shopping questions, writes nothing."""
    if agent is None:
        raise HTTPException(501, "Agent is not configured: set AZURE_OPENAI_* in backend/.env.")
    try:
        return await agent.respond(request)
    except Exception as error:  # upstream model/network failure; nothing was written
        raise HTTPException(502, f"Agent call failed: {type(error).__name__}") from error


@router.post(
    "/agent/resume",
    response_model=AgentResponse,
    tags=["provisional"],
    responses={501: {"description": "Approval/resume intentionally unimplemented"}},
)
async def agent_resume(request: ResumeRequest) -> AgentResponse:
    raise HTTPException(501, "Approval/resume is not implemented; no changes were saved.")
