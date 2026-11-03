from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from app.agent.read_agent import ReadAgent, get_read_agent
from app.core.config import settings
from app.core.dependencies import (
    get_inventory_service,
    get_media_storage_service,
    get_shopping_service,
    get_speech_service,
)
from app.models.agent import AgentRequest, AgentResponse, ResumeRequest
from app.models.base import ContractModel
from app.models.domain import InventoryItem, ShoppingItem
from app.models.speech import AllowedAudioContentType, SpeechTranscribeResponse
from app.models.storage import (
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)
from app.services.interfaces import InventoryService, ShoppingService
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.speech import SpeechToTextService

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


@router.put(
    "/staging/upload/{upload_token}",
    status_code=status.HTTP_201_CREATED,
    tags=["media", "provisional"],
    summary="[PROVISIONAL_STAGING] Receive direct binary PUT upload for provisional staging",
    responses={
        201: {"description": "Binary payload successfully staged (Azure Put Blob parity)"},
        400: {"description": "Empty payload or malformed request"},
        404: {"description": "Token not found or already consumed"},
        410: {"description": "Token has expired"},
        413: {"description": "Payload exceeds size limit"},
        501: {"description": "Storage backend does not support provisional staging PUT receiver"},
    },
)
async def receive_staged_upload(
    upload_token: str,
    request: Request,
    storage: Annotated[MediaStorageService, Depends(get_media_storage_service)],
) -> Response:
    if not hasattr(storage, "receive_staged_upload"):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Staging upload receiver is not supported by current storage backend",
        )

    payload_bytes = await request.body()

    try:
        await storage.receive_staged_upload(upload_token, payload_bytes)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        msg = str(exc)
        if "exceeds" in msg:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=msg,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=msg,
        ) from exc

    return Response(status_code=status.HTTP_201_CREATED)


SUPPORTED_AUDIO_EXTENSIONS = {".m4a", ".aac", ".mp4", ".wav", ".mp3", ".webm"}


@router.post(
    "/api/speech/transcribe",
    response_model=SpeechTranscribeResponse,
    tags=["speech"],
    summary="Transcribe audio recording to text via STT service",
    responses={
        200: {"description": "Successful transcription with language and duration"},
        400: {"description": "Empty or corrupted audio payload"},
        413: {"description": "Audio payload exceeds maximum 5MB size limit"},
        415: {"description": "Unsupported audio media type"},
    },
)
async def transcribe_speech(
    file: UploadFile = File(...),
    language: str | None = Form(None),
    speech_service: Annotated[SpeechToTextService, Depends(get_speech_service)] = None,
) -> SpeechTranscribeResponse:
    payload_bytes = await file.read()

    # 1. Zero-byte check (400)
    if not payload_bytes or len(payload_bytes) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty audio payload",
        )

    # 2. Size limit check (413) - Max 5MB
    if len(payload_bytes) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Audio payload exceeds maximum 5MB size limit",
        )

    # 3. Content type and extension validation (415)
    content_type = file.content_type or ""
    filename = file.filename or ""
    import os

    ext = os.path.splitext(filename)[1].lower()

    valid_type = (
        content_type in [v.value for v in AllowedAudioContentType]
        or ext in SUPPORTED_AUDIO_EXTENSIONS
    )
    if not valid_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported audio content type '{content_type}' or extension '{ext}'",
        )

    return await speech_service.transcribe(
        audio_bytes=payload_bytes,
        content_type=content_type,
        language=language,
    )
