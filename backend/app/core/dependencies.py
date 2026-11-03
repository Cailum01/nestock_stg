import atexit
import os
from collections.abc import Mapping

from app.services.cosmos_read import CosmosInventoryService, CosmosShoppingService
from app.services.cosmos_sdk import create_azure_cosmos_read_repository
from app.services.demo import DemoInventoryService, DemoShoppingService
from app.services.interfaces import InventoryService, ShoppingService
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.speech import MockSpeechToTextService, SpeechToTextService
from app.services.vision import (
    AzureOpenAIVisionService,
    MockVisionService,
    VisionService,
    load_vision_azure_settings,
)


def _create_domain_read_services(
    environment: Mapping[str, str] | None = None,
) -> tuple[InventoryService, ShoppingService]:
    environment = os.environ if environment is None else environment
    settings = {
        "endpoint": environment.get("NESTOCK_COSMOS_ENDPOINT"),
        "database": environment.get("NESTOCK_COSMOS_DATABASE"),
        "container": environment.get("NESTOCK_COSMOS_CONTAINER"),
    }
    configured = {key: value for key, value in settings.items() if value is not None}
    if not configured:
        return DemoInventoryService(), DemoShoppingService()
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
    return CosmosInventoryService(repository), CosmosShoppingService(repository)


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


inventory_service, shopping_service = _create_domain_read_services()
media_storage_service: MediaStorageService = LocalStagingMediaStorage()
speech_service: SpeechToTextService = MockSpeechToTextService()
vision_service: VisionService = _create_vision_service(
    inventory_service, media_storage_service
)


def get_inventory_service() -> InventoryService:
    return inventory_service


def get_shopping_service() -> ShoppingService:
    return shopping_service


def get_media_storage_service() -> MediaStorageService:
    return media_storage_service


def get_speech_service() -> SpeechToTextService:
    return speech_service


def get_vision_service() -> VisionService:
    return vision_service
