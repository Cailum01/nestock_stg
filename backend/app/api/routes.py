import logging
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.agent.cards import resume_response
from app.agent.read_agent import ReadAgent, get_read_agent
from app.core.config import settings
from app.core.dependencies import (
    get_approval_service,
    get_commerce_service,
    get_inventory_service,
    get_media_storage_service,
    get_shopping_service,
    get_speech_service,
)
from app.models.agent import AgentRequest, AgentResponse, ResumeRequest
from app.models.base import ContractModel
from app.models.commerce import CommerceSearchResponse, CommerceSearchStatus
from app.models.domain import InventoryItem, ShoppingItem
from app.models.speech import AllowedAudioContentType, SpeechTranscribeResponse
from app.models.storage import (
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)
from app.services.approvals import ApprovalNotFound, ApprovalService, InvalidSelection
from app.services.commerce.biggo import BigGoCommerceProvider
from app.services.interfaces import InventoryService, ShoppingService
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.speech import SpeechServiceError, SpeechToTextService

router = APIRouter()
logger = logging.getLogger(__name__)


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


@router.get(
    "/api/commerce/options",
    response_model=CommerceSearchResponse,
    tags=["commerce"],
    summary="[A2] 查詢外部推薦比價選項 (固定回傳3筆)",
    responses={
        200: {"description": "比價搜尋結果與推薦清單"},
    },
)
async def commerce_options(
    keyword: Annotated[str, Query(description="要比價的品名關鍵字")],
    service: Annotated[BigGoCommerceProvider, Depends(get_commerce_service)],
) -> CommerceSearchResponse:
    clean_keyword = keyword.strip()
    if not clean_keyword:
        return CommerceSearchResponse(
            status=CommerceSearchStatus.NO_MATCH,
            code="COMMERCE_EMPTY_KEYWORD",
            message="未提供品名關鍵字",
            options=[],
        )

    try:
        options = await service.search_options(clean_keyword, limit=3)
    except Exception as exc:
        logger.warning("[COMMERCE_ERROR] Search options failed: %s", exc)
        return CommerceSearchResponse(
            status=CommerceSearchStatus.UNAVAILABLE,
            code="COMMERCE_SEARCH_FAILED",
            message="外部比價服務暫時無法取得資料",
            options=[],
        )

    if not options:
        return CommerceSearchResponse(
            status=CommerceSearchStatus.NO_MATCH,
            code="COMMERCE_ITEM_NOT_FOUND",
            message="查無相符之比價推薦資料",
            options=[],
        )

    return CommerceSearchResponse(
        status=CommerceSearchStatus.SUCCESS,
        code="COMMERCE_OPTIONS_FOUND",
        message="查詢成功",
        options=options,
    )


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
    """Agent: answers questions; may return a purchase card, which writes nothing until
    the user confirms it in `/agent/resume`."""
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
    responses={
        404: {"description": "No such card in this household"},
        409: {"description": "The selection does not fit this card"},
        500: {"description": "Some writes exist without the card completing; nothing more"},
        503: {"description": "Approval cards need Cosmos (NESTOCK_COSMOS_*); nothing written"},
    },
)
async def agent_resume(
    request: ResumeRequest,
    approvals: Annotated[ApprovalService | None, Depends(get_approval_service)],
) -> AgentResponse:
    """The user's answer to a card. Writes only what the stored card holds (PR #47 §4)."""
    if approvals is None:
        raise HTTPException(503, "Approval cards need Cosmos; no changes were saved.")
    try:
        result = await approvals.resume(request)
    except ApprovalNotFound as error:
        raise HTTPException(404, "Card not found; no changes were saved.") from error
    except InvalidSelection as error:
        raise HTTPException(409, f"{error} No changes were saved.") from error
    try:
        return resume_response(result)
    except RuntimeError as error:  # receipts without completion: stop and report (§6)
        raise HTTPException(500, "Write state needs checking; nothing more was written.") from error


@router.post(
    "/api/media/upload-authorization",
    response_model=UploadAuthorizationResponse,
    tags=["media"],
)
async def create_upload_authorization(
    request: UploadAuthorizationRequest,
    http_request: Request,
    storage: Annotated[MediaStorageService, Depends(get_media_storage_service)],
) -> UploadAuthorizationResponse:
    dynamic_staging_base = f"{str(http_request.base_url).rstrip('/')}/staging/upload"
    return await storage.create_upload_authorization(
        request,
        base_staging_url=dynamic_staging_base,
    )


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
        502: {"description": "Upstream speech service failed"},
        503: {"description": "Speech service is not configured"},
    },
)
async def transcribe_speech(
    file: UploadFile = File(...),
    language: str | None = Form(None),
    speech_service: Annotated[SpeechToTextService | None, Depends(get_speech_service)] = None,
) -> SpeechTranscribeResponse:
    if speech_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Speech service is not configured. Set AZURE_SPEECH_KEY or NESTOCK_SPEECH_MOCK=1."
            ),
        )

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

    try:
        return await speech_service.transcribe(
            audio_bytes=payload_bytes,
            content_type=content_type,
            language=language,
        )
    except SpeechServiceError as exc:
        logger.error("Speech transcription failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Speech transcription failed due to upstream service error",
        ) from exc
