from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import settings
from app.core.dependencies import (
    get_inventory_service,
    get_media_storage_service,
    get_shopping_service,
)
from app.models.agent import AgentRequest, AgentResponse, ResumeRequest
from app.models.base import ContractModel
from app.models.domain import InventoryItem, ShoppingItem
from app.models.storage import (
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)
from app.services.interfaces import InventoryService, ShoppingService
from app.services.provisional_storage.interfaces import MediaStorageService

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
    responses={501: {"description": "Agent runtime intentionally unimplemented"}},
)
async def agent_message(request: AgentRequest) -> AgentResponse:
    raise HTTPException(501, "Agent runtime is not implemented in Skeleton v1.")


@router.post(
    "/agent/resume",
    response_model=AgentResponse,
    tags=["provisional"],
    responses={501: {"description": "Approval/resume intentionally unimplemented"}},
)
async def agent_resume(request: ResumeRequest) -> AgentResponse:
    raise HTTPException(501, "Approval/resume is not implemented; no changes were saved.")


@router.post(
    "/api/media/upload-authorization",
    response_model=UploadAuthorizationResponse,
    tags=["media"],
)
async def create_upload_authorization(
    request: UploadAuthorizationRequest,
    storage: Annotated[MediaStorageService, Depends(get_media_storage_service)],
) -> UploadAuthorizationResponse:
    return await storage.create_upload_authorization(request)
