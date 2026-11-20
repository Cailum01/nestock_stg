import atexit
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.agent.llm import load_vertex_settings
from app.models.identity import (
    HTTP_ID_MAX_LENGTH,
    DeviceRecord,
    HouseholdContextHeader,
    HouseholdMembership,
)
from app.services.approvals import ApprovalService
from app.services.bootstrap_issuer import (
    BootstrapIssuerRepository,
    BootstrapIssuerService,
    FirestoreBootstrapIssuerRepository,
)
from app.services.commerce import BaseCommerceProvider, FallbackCommerceProvider
from app.services.cosmos_read import (
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
    DocumentQueryClient,
)
from app.services.cosmos_sdk import create_azure_cosmos_repositories
from app.services.cosmos_write import CosmosWriteRepository, TransactionalBatchClient
from app.services.demo import DemoInventoryService, DemoShoppingService
from app.services.firestore_identity import FirestoreIdentityRepository, seed_bootstrap_challenges
from app.services.firestore_sdk import (
    FirestoreDocumentQueryClient,
    FirestoreTransactionalBatchClient,
    FirestoreWriteRepository,
)
from app.services.google_identity import GoogleIdentityService
from app.services.identity import IdentityRepository, IdentityService
from app.services.interfaces import InventoryService, ShoppingService
from app.services.media_mapping import CosmosMediaMappingStorage, MediaMappingPort
from app.services.media_retention_work import CosmosMediaRetentionWorkItemStorage
from app.services.provisional_storage.azure_blob_adapter import AzureBlobMediaStorage
from app.services.provisional_storage.gcs_adapter import GCSMediaStorage
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.shopping.intents import ShoppingIntentService
from app.services.speech import (
    SpeechToTextService,
    create_speech_service,
)
from app.services.vision import (
    VertexVisionService,
    VisionService,
)

if TYPE_CHECKING:
    from app.services.shopping.media_handoff import MediaRetentionPostCommitTrigger

BACKEND_DIR = Path(__file__).resolve().parents[2]
SKIP_DOTENV_VAR = "NESTOCK_SKIP_DOTENV"
_bearer_auth = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthorizedHousehold:
    """Authenticated install and membership authorized for the requested scope."""

    device: DeviceRecord
    membership: HouseholdMembership

    @property
    def device_id(self) -> str:
        return self.device.device_id

    @property
    def household_id(self) -> str:
        return self.membership.household_id


# WO-007 supplies the durable implementation. Never substitute demo or process-local state.
identity_repository: IdentityRepository | None = None
bootstrap_issuer_repository: BootstrapIssuerRepository | None = None


def get_identity_repository() -> IdentityRepository | None:
    return identity_repository


def get_identity_service() -> IdentityService | None:
    repository = get_identity_repository()
    return IdentityService(repository) if repository is not None else None


def get_required_identity_service(
    service: IdentityService | None = Depends(get_identity_service),
) -> IdentityService:
    if service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity repository is not configured.",
        )
    return service


def get_google_identity_service() -> GoogleIdentityService | None:
    repository = get_identity_repository()
    audience = os.environ.get("NESTOCK_GOOGLE_CLIENT_ID", "").strip()
    if repository is None or not audience:
        return None
    if not callable(getattr(repository, "provision_google_install", None)):
        return None
    return GoogleIdentityService(repository, audience=audience)  # type: ignore[arg-type]


def get_required_google_identity_service(
    service: GoogleIdentityService | None = Depends(get_google_identity_service),
) -> GoogleIdentityService:
    if service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google identity is not configured.",
        )
    return service


def get_bootstrap_issuer_service() -> BootstrapIssuerService | None:
    repository = bootstrap_issuer_repository
    if repository is None:
        return None
    subjects = frozenset(
        value.strip()
        for value in os.environ.get("NESTOCK_BOOTSTRAP_OPERATOR_SUBJECTS", "").split(",")
        if value.strip()
    )
    return BootstrapIssuerService(
        repository,
        audience=os.environ.get("NESTOCK_BOOTSTRAP_ISSUER_AUDIENCE"),
        operator_subjects=subjects,
    )


def get_required_bootstrap_issuer_service(
    service: BootstrapIssuerService | None = Depends(get_bootstrap_issuer_service),
) -> BootstrapIssuerService:
    if service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Bootstrap issuer repository is not configured.",
        )
    return service


async def get_authenticated_install(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_auth),
    service: IdentityService | None = Depends(get_identity_service),
) -> DeviceRecord:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "A per-install bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity repository is not configured.",
        )
    return await service.authenticate(credentials.credentials)


async def get_authorized_household(
    device: DeviceRecord = Depends(get_authenticated_install),
    requested_household_id: str | None = Header(
        default=None,
        alias="X-Household-Id",
        min_length=1,
        max_length=HTTP_ID_MAX_LENGTH,
    ),
    service: IdentityService = Depends(get_required_identity_service),
) -> AuthorizedHousehold:
    if requested_household_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "X-Household-Id is required.",
        )
    requested = HouseholdContextHeader.model_validate(
        {"X-Household-Id": requested_household_id}
    ).requested_household_id
    membership = await service.authorize_household(
        device_id=device.device_id,
        household_id=requested,
    )
    return AuthorizedHousehold(device=device, membership=membership)


def _load_local_env() -> None:
    """Load `backend/.env` before the Cosmos composition below reads `os.environ`.

    Real environment variables win (`override=False`). Tests set `NESTOCK_SKIP_DOTENV`
    so a developer's local `.env` never points them at a real Cosmos account.
    """
    if not os.environ.get(SKIP_DOTENV_VAR):
        load_dotenv(BACKEND_DIR / ".env", override=False)


def _create_domain_read_services(
    environment: Mapping[str, str] | None = None,
) -> tuple[InventoryService, ShoppingService]:
    inventory, shopping, _ = _create_read_composition(environment)
    return inventory, shopping


def _create_read_composition(
    environment: Mapping[str, str] | None = None,
) -> tuple[InventoryService, ShoppingService, CosmosReadRepository | None]:
    inventory, shopping, read_repository, _, _ = _create_cosmos_composition(environment)
    return inventory, shopping, read_repository


def _create_cosmos_composition(
    environment: Mapping[str, str] | None = None,
) -> tuple[
    InventoryService,
    ShoppingService,
    CosmosReadRepository | None,
    CosmosWriteRepository | None,
    DocumentQueryClient | None,
]:
    """Read services plus B1's read and write repositories and raw document reader,
    all on one Cosmos SDK client.

    Without `NESTOCK_COSMOS_*` this is the offline demo and there are no repositories.
    """
    environment = os.environ if environment is None else environment
    settings = {
        "endpoint": environment.get("NESTOCK_COSMOS_ENDPOINT"),
        "database": environment.get("NESTOCK_COSMOS_DATABASE"),
        "container": environment.get("NESTOCK_COSMOS_CONTAINER"),
    }
    configured = {key: value for key, value in settings.items() if value is not None}
    if not configured:
        return DemoInventoryService(), DemoShoppingService(), None, None, None
    if len(configured) != len(settings):
        missing = sorted(set(settings) - configured.keys())
        raise RuntimeError(
            "Cosmos read configuration is incomplete; missing "
            + ", ".join(f"NESTOCK_COSMOS_{key.upper()}" for key in missing)
            + "."
        )
    if any(not value for value in configured.values()):
        raise RuntimeError("Cosmos read configuration values must not be empty.")

    read_repository, write_repository, document_reader, close_client = (
        create_azure_cosmos_repositories(
            endpoint=settings["endpoint"] or "",
            database_name=settings["database"] or "",
            container_name=settings["container"] or "",
        )
    )
    atexit.register(close_client)
    return (
        CosmosInventoryService(read_repository),
        CosmosShoppingService(read_repository),
        read_repository,
        write_repository,
        document_reader,
    )


def _create_selected_composition(
    environment: Mapping[str, str] | None = None,
) -> tuple[
    InventoryService,
    ShoppingService,
    CosmosReadRepository | None,
    CosmosWriteRepository | None,
    DocumentQueryClient | None,
    IdentityRepository | None,
    BootstrapIssuerRepository | None,
]:
    """Resolve the explicitly selected persistence provider without demo fallback."""
    env = os.environ if environment is None else environment
    provider = env.get("NESTOCK_DATA_PROVIDER")
    cosmos_names = (
        "NESTOCK_COSMOS_ENDPOINT",
        "NESTOCK_COSMOS_DATABASE",
        "NESTOCK_COSMOS_CONTAINER",
    )
    firestore_names = (
        "NESTOCK_FIRESTORE_PROJECT_ID",
        "NESTOCK_FIRESTORE_DATABASE",
        "NESTOCK_BOOTSTRAP_CHALLENGE_HASHES",
    )
    azure_media_names = (
        "AZURE_STORAGE_CONNECTION_STRING",
        "AZURE_STORAGE_BLOB_ENDPOINT",
        "NESTOCK_STORAGE_ACCOUNT_URL",
    )
    if provider is None:
        if any(
            env.get(name)
            for name in (*firestore_names, "NESTOCK_GCS_PROJECT_ID", "NESTOCK_GCS_BUCKET")
        ):
            raise RuntimeError(
                "Google Cloud settings require explicit NESTOCK_DATA_PROVIDER=firestore."
            )
        if any(env.get(name) for name in azure_media_names) and not any(
            env.get(name) for name in cosmos_names
        ):
            raise RuntimeError(
                "Azure media settings require an explicit durable data provider; "
                "demo data is disabled for cloud media."
            )
        inventory, shopping, read_repo, write_repo, document_reader = _create_cosmos_composition(
            env
        )
        return inventory, shopping, read_repo, write_repo, document_reader, None, None

    provider = provider.strip().lower()
    if provider == "demo":
        if any(
            env.get(name)
            for name in (
                *cosmos_names,
                *firestore_names,
                "NESTOCK_GCS_PROJECT_ID",
                "NESTOCK_GCS_BUCKET",
                *azure_media_names,
            )
        ):
            raise RuntimeError("NESTOCK_DATA_PROVIDER=demo cannot be combined with cloud settings.")
        return DemoInventoryService(), DemoShoppingService(), None, None, None, None, None
    if provider == "cosmos":
        if any(env.get(name) for name in firestore_names) or any(
            env.get(name) for name in ("NESTOCK_GCS_PROJECT_ID", "NESTOCK_GCS_BUCKET")
        ):
            raise RuntimeError("Cosmos provider cannot be combined with Firestore/GCS settings.")
        inventory, shopping, read_repo, write_repo, document_reader = _create_cosmos_composition(
            env
        )
        if read_repo is None or write_repo is None or document_reader is None:
            raise RuntimeError(
                "NESTOCK_DATA_PROVIDER=cosmos requires complete NESTOCK_COSMOS_* settings."
            )
        return inventory, shopping, read_repo, write_repo, document_reader, None, None
    if provider != "firestore":
        raise RuntimeError("NESTOCK_DATA_PROVIDER must be cosmos, firestore, or demo.")

    if any(env.get(name) for name in cosmos_names):
        raise RuntimeError("Firestore provider cannot be combined with NESTOCK_COSMOS_* settings.")
    if any(env.get(name) for name in azure_media_names):
        raise RuntimeError("Firestore provider cannot be combined with Azure Blob settings.")
    missing = [name for name in firestore_names if not env.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Firestore configuration is incomplete; missing " + ", ".join(missing) + "."
        )
    if env.get("NESTOCK_MEDIA_PROVIDER", "").strip().lower() != "gcs":
        raise RuntimeError("Firestore provider requires explicit NESTOCK_MEDIA_PROVIDER=gcs.")
    challenge_hashes = tuple(
        value.strip()
        for value in env["NESTOCK_BOOTSTRAP_CHALLENGE_HASHES"].split(",")
        if value.strip()
    )
    if not challenge_hashes:
        raise RuntimeError("NESTOCK_BOOTSTRAP_CHALLENGE_HASHES must include at least one hash.")

    try:
        from google.cloud import firestore

        firestore_client = firestore.Client(
            project=env["NESTOCK_FIRESTORE_PROJECT_ID"].strip(),
            database=env["NESTOCK_FIRESTORE_DATABASE"].strip(),
        )
        seed_bootstrap_challenges(firestore_client, challenge_hashes)
    except Exception as error:
        raise RuntimeError(
            "Firestore initialization failed; verify ADC credentials and provider settings."
        ) from error
    atexit.register(firestore_client.close)

    query_client = FirestoreDocumentQueryClient(firestore_client)
    batch_client = FirestoreTransactionalBatchClient(firestore_client)
    read_repository = CosmosReadRepository(query_client)
    write_repository = FirestoreWriteRepository(
        firestore_client,
        query_client=query_client,
        transactional_client=batch_client,
    )
    return (
        CosmosInventoryService(read_repository),
        CosmosShoppingService(read_repository),
        read_repository,
        write_repository,
        query_client,
        FirestoreIdentityRepository(firestore_client),
        FirestoreBootstrapIssuerRepository(firestore_client),
    )


def _create_vision_service(
    inventory: InventoryService,
    media: MediaStorageService,
    environment: Mapping[str, str] | None = None,
) -> VisionService | None:
    settings = load_vertex_settings(environment=environment)
    if settings is not None:
        return VertexVisionService(
            settings=settings,
            inventory_service=inventory,
            media_storage=media,
        )
    return None


logger = logging.getLogger(__name__)


def _resolve_vision_service_for_media() -> VisionService | None:
    return get_vision_service()


def _create_media_storage_service(
    read_repository: CosmosReadRepository | None = None,
    write_client: TransactionalBatchClient | None = None,
    mapping_port: MediaMappingPort | None = None,
    environment: Mapping[str, str] | None = None,
) -> MediaStorageService:
    env = os.environ if environment is None else environment
    base_url = env.get("NESTOCK_STAGING_BASE_URL")
    if mapping_port is None and read_repository is not None:
        client_obj = getattr(read_repository, "_client", None) or getattr(
            read_repository, "_query_client", None
        )
        if write_client is not None:
            mapping_port = CosmosMediaMappingStorage(
                read_repository=read_repository,
                write_client=write_client,
            )
        elif hasattr(client_obj, "execute_batch"):
            mapping_port = CosmosMediaMappingStorage(
                read_repository=read_repository,
                write_client=client_obj,  # type: ignore[arg-type]
            )
        elif hasattr(client_obj, "_container"):
            from app.services.cosmos_sdk import AzureCosmosTransactionalBatchClient

            mapping_port = CosmosMediaMappingStorage(
                read_repository=read_repository,
                write_client=AzureCosmosTransactionalBatchClient(client_obj._container),
            )

    selected_provider = env.get("NESTOCK_MEDIA_PROVIDER")
    if selected_provider is not None:
        selected_provider = selected_provider.strip().lower()
        if selected_provider not in {"azure", "gcs", "local"}:
            raise RuntimeError("NESTOCK_MEDIA_PROVIDER must be azure, gcs, or local.")
    data_provider = env.get("NESTOCK_DATA_PROVIDER", "").strip().lower()
    if data_provider == "firestore" and selected_provider != "gcs":
        raise RuntimeError("Firestore provider requires explicit NESTOCK_MEDIA_PROVIDER=gcs.")
    if selected_provider == "gcs":
        if any(
            env.get(name)
            for name in (
                "AZURE_STORAGE_CONNECTION_STRING",
                "AZURE_STORAGE_BLOB_ENDPOINT",
                "NESTOCK_STORAGE_ACCOUNT_URL",
            )
        ):
            raise RuntimeError("GCS media provider cannot be combined with Azure Blob settings.")
        required = ("NESTOCK_GCS_PROJECT_ID", "NESTOCK_GCS_BUCKET")
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise RuntimeError(
                "GCS configuration is incomplete; missing " + ", ".join(missing) + "."
            )
        if mapping_port is None:
            raise RuntimeError("GCS media requires a durable household media-mapping repository.")
        if env.get("NESTOCK_DATA_PROVIDER", "").strip().lower() != "firestore":
            raise RuntimeError("GCS media requires NESTOCK_DATA_PROVIDER=firestore.")
        parsed_staging_url = urlsplit(base_url or "")
        if (
            parsed_staging_url.scheme != "https"
            or not parsed_staging_url.hostname
            or parsed_staging_url.username
            or parsed_staging_url.password
            or parsed_staging_url.path.rstrip("/") != "/staging/upload"
            or parsed_staging_url.query
            or parsed_staging_url.fragment
        ):
            raise RuntimeError(
                "GCS media requires NESTOCK_STAGING_BASE_URL to be a fixed HTTPS "
                "URL ending in /staging/upload."
            )
        try:
            from google.cloud import storage

            storage_client = storage.Client(project=env["NESTOCK_GCS_PROJECT_ID"].strip())
            bucket = storage_client.bucket(env["NESTOCK_GCS_BUCKET"].strip())
            bucket.reload()
            iam = bucket.iam_configuration
            if not getattr(iam, "uniform_bucket_level_access_enabled", False):
                raise RuntimeError("GCS bucket must enforce uniform bucket-level access.")
            if getattr(iam, "public_access_prevention", None) != "enforced":
                raise RuntimeError("GCS bucket must enforce public access prevention.")
            atexit.register(storage_client.close)
            return GCSMediaStorage(
                bucket=bucket,
                base_staging_url=base_url,
                mapping_port=mapping_port,
                vision_service=_resolve_vision_service_for_media,
            )
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(
                "GCS initialization failed; verify ADC credentials, bucket access, and settings."
            ) from error
    if selected_provider == "local":
        if any(
            env.get(name)
            for name in (
                "AZURE_STORAGE_CONNECTION_STRING",
                "AZURE_STORAGE_BLOB_ENDPOINT",
                "NESTOCK_STORAGE_ACCOUNT_URL",
                "NESTOCK_GCS_PROJECT_ID",
                "NESTOCK_GCS_BUCKET",
            )
        ):
            raise RuntimeError(
                "Local media provider cannot be combined with cloud storage settings."
            )
        return LocalStagingMediaStorage(
            base_staging_url=base_url,
            mapping_port=mapping_port,
            vision_service=_resolve_vision_service_for_media,
        )

    # 偵測雲端 Azure Blob Storage 配置 (支援連線字串或 Endpoint + DefaultAzureCredential)
    conn_str = env.get("AZURE_STORAGE_CONNECTION_STRING")
    blob_endpoint = env.get("AZURE_STORAGE_BLOB_ENDPOINT") or env.get("NESTOCK_STORAGE_ACCOUNT_URL")
    container_name = env.get("AZURE_STORAGE_CONTAINER_NAME") or "media"
    if selected_provider is None and (conn_str or blob_endpoint):
        raise RuntimeError("Azure Blob settings require explicit NESTOCK_MEDIA_PROVIDER=azure.")
    if selected_provider == "azure":
        if (
            env.get("NESTOCK_GCS_PROJECT_ID")
            or env.get("NESTOCK_GCS_BUCKET")
            or data_provider == "firestore"
        ):
            raise RuntimeError(
                "Azure media provider cannot be combined with Firestore/GCS settings."
            )
        if not conn_str and not blob_endpoint:
            raise RuntimeError(
                "Azure media provider requires AZURE_STORAGE_CONNECTION_STRING or "
                "AZURE_STORAGE_BLOB_ENDPOINT."
            )
    elif env.get("NESTOCK_GCS_PROJECT_ID") or env.get("NESTOCK_GCS_BUCKET"):
        raise RuntimeError("GCS settings require explicit NESTOCK_MEDIA_PROVIDER=gcs.")

    kwargs: dict[str, Any] = {
        "container_name": container_name,
        "base_staging_url": base_url,
        "mapping_port": mapping_port,
        "vision_service": _resolve_vision_service_for_media,
    }

    if conn_str:
        try:
            from azure.storage.blob import BlobServiceClient

            blob_client = BlobServiceClient.from_connection_string(conn_str)
            return AzureBlobMediaStorage(blob_service_client=blob_client, **kwargs)
        except Exception as exc:
            logger.error("Failed to initialize Azure Blob Storage from connection string.")
            raise RuntimeError("Azure Blob Storage configuration failed.") from exc
    elif blob_endpoint:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            credential = DefaultAzureCredential()
            blob_client = BlobServiceClient(account_url=blob_endpoint, credential=credential)
            return AzureBlobMediaStorage(blob_service_client=blob_client, **kwargs)
        except Exception as exc:
            logger.error("Failed to initialize Azure Blob Storage from blob endpoint.")
            raise RuntimeError("Azure Blob Storage configuration failed.") from exc

    return LocalStagingMediaStorage(
        base_staging_url=base_url,
        mapping_port=mapping_port,
        vision_service=_resolve_vision_service_for_media,
    )


_load_local_env()
(
    inventory_service,
    shopping_service,
    cosmos_read_repository,
    cosmos_write_repository,
    cosmos_document_reader,
    identity_repository,
    bootstrap_issuer_repository,
) = _create_selected_composition()
media_storage_service: MediaStorageService = _create_media_storage_service(
    read_repository=cosmos_read_repository,
    write_client=(
        cosmos_write_repository.transactional_batch_client
        if cosmos_write_repository is not None
        else None
    ),
)
_speech_service: SpeechToTextService | None = None
_speech_service_resolved: bool = False
_speech_lock = threading.Lock()
_vision_service: VisionService | None = None
_vision_service_resolved: bool = False


def get_inventory_service() -> InventoryService:
    return inventory_service


def get_shopping_service() -> ShoppingService:
    return shopping_service


def get_shopping_intent_service() -> ShoppingIntentService | None:
    """Direct-add writer using the selected provider's shared durable write port."""
    if cosmos_read_repository is None or cosmos_write_repository is None:
        return None
    writer = cosmos_write_repository
    if isinstance(cosmos_write_repository, FirestoreWriteRepository):
        # Firestore's approval repository requires a completed approval card for
        # its specialized transaction path. A direct, explicitly approved Add
        # instead uses the shared atomic batch port and standard operation receipts.
        writer = CosmosWriteRepository(
            cosmos_write_repository.transactional_batch_client,
            query_client=cosmos_document_reader,
        )
    return ShoppingIntentService(writer, cosmos_read_repository)


def get_cosmos_read_repository() -> CosmosReadRepository | None:
    return cosmos_read_repository


def get_media_storage_service() -> MediaStorageService:
    return media_storage_service


def get_speech_service() -> SpeechToTextService | None:
    """FastAPI dependency for SpeechToTextService.

    Thread-safe lazy resolution of AzureSpeechService when AZURE_SPEECH_KEY
    is configured, MockSpeechToTextService when NESTOCK_SPEECH_MOCK=1,
    or None if speech service is unconfigured.
    """
    global _speech_service, _speech_service_resolved
    if not _speech_service_resolved:
        with _speech_lock:
            if not _speech_service_resolved:
                _speech_service = create_speech_service(environment=os.environ)
                _speech_service_resolved = True
    return _speech_service


def _reset_speech_service_for_test() -> None:
    """Reset speech singleton state for test isolation."""
    global _speech_service, _speech_service_resolved
    with _speech_lock:
        _speech_service = None
        _speech_service_resolved = False


def get_vision_service(environment: Mapping[str, str] | None = None) -> VisionService | None:
    """FastAPI dependency for VisionService.

    Lazily instantiates the vision provider on first invocation rather than
    module import time. This ensures runtime environment variables from .env
    are available when resolving Vertex references for ADC.
    Returns None if real Vision provider is not configured.
    """
    global _vision_service, _vision_service_resolved
    if environment is not None:
        return _create_vision_service(
            get_inventory_service(),
            get_media_storage_service(),
            environment=environment,
        )
    if not _vision_service_resolved:
        _vision_service = _create_vision_service(
            get_inventory_service(),
            get_media_storage_service(),
        )
        _vision_service_resolved = True
    return _vision_service


def _reset_vision_service_for_test() -> None:
    """Reset singleton state for test isolation."""
    global _vision_service, _vision_service_resolved
    _vision_service = None
    _vision_service_resolved = False


_commerce_service: BaseCommerceProvider | None = None
_commerce_service_resolved: bool = False
_commerce_lock = threading.Lock()


def get_commerce_service() -> BaseCommerceProvider:
    """FastAPI dependency for BaseCommerceProvider.

    Thread-safe lazy resolution of FallbackCommerceProvider.
    """
    global _commerce_service, _commerce_service_resolved
    if not _commerce_service_resolved:
        with _commerce_lock:
            if not _commerce_service_resolved:
                _commerce_service = FallbackCommerceProvider()
                _commerce_service_resolved = True
    return _commerce_service


def _reset_commerce_service_for_test() -> None:
    """Reset commerce singleton state for test isolation."""
    global _commerce_service, _commerce_service_resolved
    with _commerce_lock:
        _commerce_service = None
        _commerce_service_resolved = False


_approval_service: ApprovalService | None = None
_approval_service_resolved: bool = False


def get_approval_service() -> ApprovalService | None:
    """A1 confirmation cards (`contracts/approval-lifecycle.md`). Cosmos only.

    Cards are stored in `household-data` (Issue #41) and written through B1's
    `CosmosWriteRepository`, so without `NESTOCK_COSMOS_*` there is none: the Agent
    offers no card tool and `/agent/resume` answers 503. Made on first use, from the
    repositories and raw reader the composition above made on one SDK client.
    """
    global _approval_service, _approval_service_resolved
    if not _approval_service_resolved:
        _approval_service = _create_approval_service(
            cosmos_read_repository, cosmos_write_repository, cosmos_document_reader
        )
        _approval_service_resolved = True
    return _approval_service


def _create_approval_service(
    read_repository: CosmosReadRepository | None,
    write_repository: CosmosWriteRepository | None,
    document_reader: DocumentQueryClient | None,
) -> ApprovalService | None:
    """Route stored approval cards to their domain-specific write builders.

    A1's `CardInputResolver` turns purchase cards back into C1's inputs through the
    read repository; C1 reads whole stored documents through the raw reader so
    storage-only metadata survives replacements. New-item cards use A1's resolver
    and B1's atomic no-Shopping writer; the router reads the inventory again first, so
    an item added since the card was made is not added twice (PR #187).
    """
    if read_repository is None or write_repository is None or document_reader is None:
        return None
    from app.services.card_writes import CardWriteRouter
    from app.services.photo_item_writes import PhotoItemWriteBuilder
    from app.services.purchase_inputs import CardInputResolver, PhotoInputResolver
    from app.services.shopping.media import B1MediaRetentionPlanner
    from app.services.shopping.media_handoff import B1MediaRetentionWorkItemAdapter
    from app.services.shopping.workflow import ShoppingWriteBuilder

    write_client = write_repository.transactional_batch_client
    media_mapping = CosmosMediaMappingStorage(
        read_repository=read_repository,
        write_client=write_client,
    )
    media_work_items = B1MediaRetentionWorkItemAdapter(
        CosmosMediaRetentionWorkItemStorage(
            write_client=write_client,
            query_client=document_reader,
        )
    )
    builder = CardWriteRouter(
        purchases=ShoppingWriteBuilder(
            document_reader,
            CardInputResolver(read_repository),
            media_retention=B1MediaRetentionPlanner(media_mapping),
            media_work_items=media_work_items,
        ),
        photo_items=PhotoItemWriteBuilder(
            PhotoInputResolver(),
            media_mapping_storage=media_mapping,
        ),
        household_items=read_repository,
    )
    return ApprovalService(write_repository, builder)


_media_retention_trigger: "MediaRetentionPostCommitTrigger | None" = None
_media_retention_trigger_resolved: bool = False


def get_media_retention_trigger() -> "MediaRetentionPostCommitTrigger | None":
    """C1's post-commit photo retention (PR #201, Issue #176), run after `/agent/resume`.

    Cosmos only, like the approval service: without `NESTOCK_COSMOS_*` there is none
    and confirming moves no photo. Made on first use from the same SDK client.
    """
    global _media_retention_trigger, _media_retention_trigger_resolved
    if not _media_retention_trigger_resolved:
        _media_retention_trigger = _create_media_retention_trigger(
            cosmos_write_repository, cosmos_document_reader, media_storage_service
        )
        _media_retention_trigger_resolved = True
    return _media_retention_trigger


def _reset_media_retention_trigger_for_test() -> None:
    """Reset the trigger singleton for test isolation (PR #203 A2 review)."""
    global _media_retention_trigger, _media_retention_trigger_resolved
    _media_retention_trigger = None
    _media_retention_trigger_resolved = False


def _create_media_retention_trigger(
    write_repository: CosmosWriteRepository | None,
    document_reader: DocumentQueryClient | None,
    media: MediaStorageService,
) -> "MediaRetentionPostCommitTrigger | None":
    """C1's worker over B1's work-item storage and A2's idempotent `retain_media`.

    The work items are the ones the approval batch wrote (`_create_approval_service`),
    read and updated through the same Cosmos client.
    """
    if write_repository is None or document_reader is None:
        return None
    from app.services.shopping.media_handoff import (
        B1MediaRetentionWorkItemAdapter,
        MediaRetentionPostCommitTrigger,
        MediaRetentionRetryWorker,
    )

    work_items = B1MediaRetentionWorkItemAdapter(
        CosmosMediaRetentionWorkItemStorage(
            write_client=write_repository.transactional_batch_client,
            query_client=document_reader,
        )
    )
    return MediaRetentionPostCommitTrigger(MediaRetentionRetryWorker(work_items, media))
