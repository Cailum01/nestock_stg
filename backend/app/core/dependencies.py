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
from app.services.demo import DemoInventoryService, DemoShoppingService
from app.services.interfaces import InventoryService, ShoppingService
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.speech import (
    SpeechToTextService,
    create_speech_service,
)
from app.services.vision import (
    AzureOpenAIVisionService,
    MockVisionService,
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
) -> VisionService:
    settings = load_vision_azure_settings(environment=environment)
    if settings is not None:
        return AzureOpenAIVisionService(
            settings=settings,
            inventory_service=inventory,
            media_storage=media,
        )
    return MockVisionService(inventory_service=inventory)


inventory_service, shopping_service, cosmos_read_repository = _create_read_composition()
media_storage_service: MediaStorageService = LocalStagingMediaStorage()
_speech_service: SpeechToTextService | None = None
_speech_lock = threading.Lock()
_vision_service: VisionService | None = None


def get_inventory_service() -> InventoryService:
    return inventory_service


def get_shopping_service() -> ShoppingService:
    return shopping_service


def get_cosmos_read_repository() -> CosmosReadRepository | None:
    return cosmos_read_repository


def get_media_storage_service() -> MediaStorageService:
    return media_storage_service


def get_speech_service() -> SpeechToTextService:
    """FastAPI dependency for SpeechToTextService.

    Thread-safe lazy resolution of AzureSpeechService when AZURE_SPEECH_KEY
    is configured, or MockSpeechToTextService otherwise.
    """
    global _speech_service
    if _speech_service is None:
        with _speech_lock:
            if _speech_service is None:
                _speech_service = create_speech_service()
    return _speech_service


def _reset_speech_service_for_test() -> None:
    """Reset speech singleton state for test isolation."""
    global _speech_service
    with _speech_lock:
        _speech_service = None


def get_vision_service(environment: Mapping[str, str] | None = None) -> VisionService:
    """FastAPI dependency for VisionService.

    Lazily instantiates the vision provider on first invocation rather than
    module import time. This ensures runtime environment variables from .env
    are fully available when resolving Azure OpenAI credentials.
    """
    global _vision_service
    if environment is not None:
        return _create_vision_service(
            get_inventory_service(),
            get_media_storage_service(),
            environment=environment,
        )
    if _vision_service is None:
        _vision_service = _create_vision_service(
            get_inventory_service(),
            get_media_storage_service(),
        )
    return _vision_service


def _reset_vision_service_for_test() -> None:
    """Reset singleton state for test isolation."""
    global _vision_service
    _vision_service = None
