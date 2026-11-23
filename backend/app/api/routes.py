import logging
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
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
from app.api.bootstrap_issuer import router as bootstrap_issuer_router
from app.api.identity import IdentityErrorRoute
from app.api.identity import router as identity_router
from app.core.dependencies import (
    AuthorizedHousehold,
    get_approval_service,
    get_authenticated_install,
    get_authorized_household,
    get_commerce_service,
    get_inventory_service,
    get_media_retention_trigger,
    get_media_storage_service,
    get_required_identity_service,
    get_shopping_intent_service,
    get_shopping_service,
    get_speech_service,
)
from app.models.agent import AgentRequest, AgentResponse, ResumeRequest
from app.models.base import ContractModel
from app.models.commerce import CommerceSearchResponse, CommerceSearchStatus
from app.models.domain import InventoryItem, ShoppingItem
from app.models.identity import DeviceRecord
from app.models.shopping import AddShoppingIntentRequest
from app.models.speech import AllowedAudioContentType, SpeechTranscribeResponse
from app.models.storage import (
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)
from app.services.approvals import (
    ApprovalNotFound,
    ApprovalService,
    InvalidSelection,
    ResumeOutcome,
)
from app.services.commerce import BaseCommerceProvider
from app.services.cosmos_write import OperationIdConflict, WriteStatus
from app.services.identity import IdentityService
from app.services.interfaces import InventoryService, ShoppingService
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.shopping.intents import (
    AddShoppingIntent,
    ShoppingIntentError,
    ShoppingIntentService,
)
from app.services.shopping.media_handoff import MediaRetentionPostCommitTrigger
from app.services.speech import SpeechServiceError, SpeechToTextService
from app.version import APP_VERSION_LABEL

router = APIRouter(route_class=IdentityErrorRoute)
router.include_router(identity_router)
router.include_router(bootstrap_issuer_router)
logger = logging.getLogger(__name__)


class HealthResponse(ContractModel):
    status: Literal["ok"] = "ok"
    version: str


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse(version=APP_VERSION_LABEL)


@router.get("/inventory", response_model=list[InventoryItem], tags=["demo"])
async def inventory(
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
) -> list[InventoryItem]:
    return await service.list_items(context.household_id)


@router.get("/shopping", response_model=list[ShoppingItem], tags=["demo"])
async def shopping(
    service: Annotated[ShoppingService, Depends(get_shopping_service)],
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
) -> list[ShoppingItem]:
    return await service.list_items(context.household_id)


@router.post(
    "/shopping",
    response_model=ShoppingItem,
    status_code=status.HTTP_201_CREATED,
    tags=["shopping"],
    responses={
        400: {"description": "X-Household-Id is required"},
        401: {"description": "An authenticated install is required"},
        403: {"description": "The install is not a member of this household"},
        200: {"description": "This request_id and payload were already applied"},
        422: {"description": "The Shopping request is invalid"},
        409: {"description": "request_id was previously used with different values"},
        503: {"description": "Shopping writes are not configured; nothing was saved"},
    },
)
async def add_shopping_intent(
    request: AddShoppingIntentRequest,
    response: Response,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    service: Annotated[ShoppingIntentService | None, Depends(get_shopping_intent_service)],
) -> ShoppingItem:
    """Persist one pending item after explicit Add within the authorized household."""
    if service is None:
        raise HTTPException(503, "Shopping writes are not configured; no changes were saved.")
    try:
        result = await service.add(
            AddShoppingIntent(
                household_id=context.household_id,
                request_id=request.request_id,
                name=request.name,
                quantity=request.quantity,
                unit=request.unit,
            )
        )
    except ShoppingIntentError as error:
        raise HTTPException(422, str(error)) from error
    except OperationIdConflict as error:
        raise HTTPException(409, "request_id was already used with different values.") from error
    if result.status is WriteStatus.ALREADY_APPLIED:
        response.status_code = status.HTTP_200_OK
    return result.item


@router.get(
    "/api/commerce/options",
    response_model=CommerceSearchResponse,
    tags=["commerce"],
    summary="查詢外部推薦比價選項 (固定回傳3筆)",
    responses={
        200: {"description": "比價搜尋結果與推薦清單"},
    },
)
async def commerce_options(
    keyword: Annotated[str, Query(description="要比價的品名關鍵字")],
    _install: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    service: Annotated[BaseCommerceProvider, Depends(get_commerce_service)],
) -> CommerceSearchResponse:
    del _install
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
        501: {"description": "Vertex AI is not configured"},
        502: {"description": "Vertex AI call failed"},
    },
)
async def agent_message(
    request: AgentRequest,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    identity: Annotated[IdentityService, Depends(get_required_identity_service)],
    agent: Annotated[ReadAgent | None, Depends(get_read_agent)],
) -> AgentResponse:
    """Agent: answers questions; may return a purchase card, which writes nothing until
    the user confirms it in `/agent/resume`."""
    identity.require_matching_household(request.household_id, context.household_id)
    if agent is None:
        raise HTTPException(
            501,
            "Agent is not configured: set NESTOCK_VERTEX_PROJECT_ID or "
            "GOOGLE_CLOUD_PROJECT and configure ADC.",
        )
    try:
        scoped_request = request.model_copy(update={"household_id": context.household_id})
        return await agent.respond(scoped_request)
    except Exception as error:  # upstream model/network failure; nothing was written
        raise HTTPException(502, f"Agent call failed: {type(error).__name__}") from error


@router.get(
    "/agent/open-card",
    response_model=AgentResponse,
    tags=["provisional"],
    responses={
        501: {"description": "Vertex AI is not configured"},
        503: {"description": "Open cards could not be listed right now; try again later"},
    },
)
async def agent_open_card(
    household_id: Annotated[str, Query(min_length=1)],
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    identity: Annotated[IdentityService, Depends(get_required_identity_service)],
    agent: Annotated[ReadAgent | None, Depends(get_read_agent)],
) -> AgentResponse:
    """Return the newest unanswered card to an authorized household without a model call."""
    identity.require_matching_household(household_id, context.household_id)
    if agent is None:
        raise HTTPException(
            501,
            "Agent is not configured: set NESTOCK_VERTEX_PROJECT_ID or "
            "GOOGLE_CLOUD_PROJECT and configure ADC.",
        )
    reply = await agent.open_card(context.household_id)
    if reply is None:
        raise HTTPException(503, "Open cards could not be listed; try again later.")
    return reply


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
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    identity: Annotated[IdentityService, Depends(get_required_identity_service)],
    approvals: Annotated[ApprovalService | None, Depends(get_approval_service)],
    background: BackgroundTasks,
    retention: Annotated[
        MediaRetentionPostCommitTrigger | None, Depends(get_media_retention_trigger)
    ],
) -> AgentResponse:
    """The user's answer to a card. Writes only what the stored card holds (PR #47 §4).

    After the reply, C1's post-commit trigger moves the confirmed photos out of staging
    (Issue #176, PR #201) in the background, so the user does not wait for it. It runs
    only after the first write (`completed`): a repeated confirm (`already_completed`)
    does not start a second run of the same work at the same time (PR #203 A2 review).
    A failure leaves the work item pending for the next written confirm in the
    household and never changes the reply.
    """
    identity.require_matching_household(request.household_id, context.household_id)
    if approvals is None:
        raise HTTPException(503, "Approval cards need Cosmos; no changes were saved.")
    scoped_request = request.model_copy(update={"household_id": context.household_id})
    try:
        result = await approvals.resume(scoped_request)
    except ApprovalNotFound as error:
        raise HTTPException(404, "Card not found; no changes were saved.") from error
    except InvalidSelection as error:
        raise HTTPException(409, f"{error} No changes were saved.") from error
    try:
        response = resume_response(result)
    except RuntimeError as error:  # receipts without completion: stop and report (§6)
        raise HTTPException(500, "Write state needs checking; nothing more was written.") from error
    if retention is not None and result.outcome is ResumeOutcome.COMPLETED:
        background.add_task(
            retention.after_resume, household_id=request.household_id, result=result
        )
    return response


@router.post(
    "/api/media/upload-authorization",
    response_model=UploadAuthorizationResponse,
    tags=["media"],
)
async def create_upload_authorization(
    request: UploadAuthorizationRequest,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    identity: Annotated[IdentityService, Depends(get_required_identity_service)],
    storage: Annotated[MediaStorageService, Depends(get_media_storage_service)],
) -> UploadAuthorizationResponse:
    identity.require_matching_household(request.household_id, context.household_id)
    scoped_request = request.model_copy(update={"household_id": context.household_id})
    return await storage.create_upload_authorization(
        scoped_request,
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
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    storage: Annotated[MediaStorageService, Depends(get_media_storage_service)],
) -> Response:
    if not hasattr(storage, "receive_staged_upload"):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Staging upload receiver is not supported by current storage backend",
        )
    if not hasattr(storage, "is_upload_token_scoped_to"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend cannot validate upload household scope",
        )
    if not await storage.is_upload_token_scoped_to(upload_token, context.household_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload authorization not found for this household",
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
    _install: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    file: UploadFile = File(...),
    language: str | None = Form(None),
    speech_service: Annotated[SpeechToTextService | None, Depends(get_speech_service)] = None,
) -> SpeechTranscribeResponse:
    del _install
    if speech_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Speech service is not configured. Set NESTOCK_VERTEX_PROJECT_ID or "
                "GOOGLE_CLOUD_PROJECT and configure ADC, or explicitly enable "
                "NESTOCK_SPEECH_MOCK=1."
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
