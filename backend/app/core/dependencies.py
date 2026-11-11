import atexit
import os
import threading
from collections.abc import Mapping

from app.services.cosmos_read import (
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
)
from app.services.cosmos_sdk import create_azure_cosmos_read_repository
from app.services.cosmos_write import TransactionalBatchClient
from app.services.demo import DemoInventoryService, DemoShoppingService
from app.services.interfaces import InventoryService, ShoppingService
from app.services.media_mapping import CosmosMediaMappingStorage, MediaMappingPort
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
    environment = os.environ if environment is None else environment
    settings = {
        "endpoint": environment.get("NESTOCK_COSMOS_ENDPOINT"),
        "database": environment.get("NESTOCK_COSMOS_DATABASE"),
        "container": environment.get("NESTOCK_COSMOS_CONTAINER"),
    }
    configured = {key: value for key, value in settings.items() if value is not None}
    if not configured:
        return DemoInventoryService(), DemoShoppingService(), None
    if len(configured) != len(settings):
        missing = sorted(set(settings) - configured.keys())
        raise RuntimeError(
            "Cosmos read configuration is incomplete; missing "
            + ", ".join(f"NESTOCK_COSMOS_{key.upper()}" for key in missing)
            + "."
        )
    if any(not value for value in configured.values()):
        raise RuntimeError("Cosmos read configuration values must not be empty.")

    repository, close_client = create_azure_cosmos_read_repository(
        endpoint=settings["endpoint"] or "",
        database_name=settings["database"] or "",
        container_name=settings["container"] or "",
    )
    atexit.register(close_client)
    return CosmosInventoryService(repository), CosmosShoppingService(repository), repository


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


def _create_media_storage_service(
    read_repository: CosmosReadRepository | None = None,
    write_client: TransactionalBatchClient | None = None,
    mapping_port: MediaMappingPort | None = None,
    environment: Mapping[str, str] | None = None,
) -> MediaStorageService:
    env = os.environ if environment is None else environment
    base_url = env.get("NESTOCK_STAGING_BASE_URL")
    if mapping_port is None and read_repository is not None:
        if write_client is not None:
            mapping_port = CosmosMediaMappingStorage(
                read_repository=read_repository,
                write_client=write_client,
            )
        elif isinstance(getattr(read_repository, "_query_client", None), TransactionalBatchClient):
            mapping_port = CosmosMediaMappingStorage(
                read_repository=read_repository,
                write_client=read_repository._query_client,  # type: ignore[arg-type]
            )
        elif hasattr(getattr(read_repository, "_query_client", None), "_container"):
            from app.services.cosmos_sdk import AzureCosmosTransactionalBatchClient

            mapping_port = CosmosMediaMappingStorage(
                read_repository=read_repository,
                write_client=AzureCosmosTransactionalBatchClient(
                    read_repository._query_client._container
                ),
            )
    return LocalStagingMediaStorage(base_staging_url=base_url, mapping_port=mapping_port)


inventory_service, shopping_service, cosmos_read_repository = _create_read_composition()
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
