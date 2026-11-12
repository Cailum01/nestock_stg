import atexit
import logging
import os
import threading
from collections.abc import Mapping

from app.services.approvals import ApprovalService
from app.services.cosmos_read import (
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
    DocumentQueryClient,
)
from app.services.cosmos_sdk import create_azure_cosmos_repositories
from app.services.cosmos_write import CosmosWriteRepository, TransactionalBatchClient
from app.services.demo import DemoInventoryService, DemoShoppingService
from app.services.interfaces import InventoryService, ShoppingService
from app.services.media_mapping import CosmosMediaMappingStorage, MediaMappingPort
from app.services.provisional_storage.azure_blob_adapter import AzureBlobMediaStorage
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.speech import (
    SpeechToTextService,
    create_speech_service,
)
from app.services.vision import (
    AzureOpenAIVisionService,
    VisionService,
    load_vision_azure_settings,
)


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


def _create_vision_service(
    inventory: InventoryService,
    media: MediaStorageService,
    environment: Mapping[str, str] | None = None,
) -> VisionService | None:
    settings = load_vision_azure_settings(environment=environment)
    if settings is not None:
        return AzureOpenAIVisionService(
            settings=settings,
            inventory_service=inventory,
            media_storage=media,
        )
    return None


logger = logging.getLogger(__name__)


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

    # 偵測雲端 Azure Blob Storage 配置 (支援連線字串或 Endpoint + DefaultAzureCredential)
    conn_str = env.get("AZURE_STORAGE_CONNECTION_STRING")
    blob_endpoint = env.get("AZURE_STORAGE_BLOB_ENDPOINT")
    container_name = env.get("AZURE_STORAGE_CONTAINER_NAME", "media")

    if conn_str:
        try:
            from azure.storage.blob import BlobServiceClient

            blob_client = BlobServiceClient.from_connection_string(conn_str)
            return AzureBlobMediaStorage(
                blob_service_client=blob_client,
                container_name=container_name,
                base_staging_url=base_url,
                mapping_port=mapping_port,
            )
        except Exception as exc:
            logger.warning(
                "Failed to initialize AzureBlobMediaStorage from connection string: %s", exc
            )
    elif blob_endpoint:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            credential = DefaultAzureCredential()
            blob_client = BlobServiceClient(account_url=blob_endpoint, credential=credential)
            return AzureBlobMediaStorage(
                blob_service_client=blob_client,
                container_name=container_name,
                base_staging_url=base_url,
                mapping_port=mapping_port,
            )
        except Exception as exc:
            logger.warning("Failed to initialize AzureBlobMediaStorage from blob endpoint: %s", exc)

    return LocalStagingMediaStorage(base_staging_url=base_url, mapping_port=mapping_port)


(
    inventory_service,
    shopping_service,
    cosmos_read_repository,
    cosmos_write_repository,
    cosmos_document_reader,
) = _create_cosmos_composition()
media_storage_service: MediaStorageService = _create_media_storage_service(
    read_repository=cosmos_read_repository
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
                _speech_service = create_speech_service()
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
    are fully available when resolving Azure OpenAI credentials.
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
    """Purchase cards are written by C1's `ShoppingWriteBuilder` (Issue #60).

    A1's `CardInputResolver` turns the stored card back into C1's purchase inputs
    through the read repository; C1 reads whole stored documents through the raw
    reader so storage-only metadata survives its replacements.
    """
    if read_repository is None or write_repository is None or document_reader is None:
        return None
    from app.services.purchase_inputs import CardInputResolver
    from app.services.shopping.workflow import ShoppingWriteBuilder

    builder = ShoppingWriteBuilder(document_reader, CardInputResolver(read_repository))
    return ApprovalService(write_repository, builder)
