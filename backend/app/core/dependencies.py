import atexit
import os
import threading
from collections.abc import Mapping

from app.services.approvals import ApprovalService
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
    environment: Mapping[str, str] | None = None,
) -> MediaStorageService:
    env = os.environ if environment is None else environment
    base_url = env.get("NESTOCK_STAGING_BASE_URL")
    return LocalStagingMediaStorage(base_staging_url=base_url)


inventory_service, shopping_service, cosmos_read_repository = _create_read_composition()
media_storage_service: MediaStorageService = _create_media_storage_service()
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
    offers no card tool and `/agent/resume` answers 503. Made on first use. Writes use
    a second SDK client next to the read one, until both come from one client.
    """
    global _approval_service, _approval_service_resolved
    if not _approval_service_resolved:
        _approval_service = _create_approval_service(cosmos_read_repository)
        _approval_service_resolved = True
    return _approval_service


def _create_approval_service(
    read_repository: CosmosReadRepository | None,
    environment: Mapping[str, str] | None = None,
) -> ApprovalService | None:
    if read_repository is None:
        return None
    # Imported here so the demo path never loads the Azure SDK.
    from app.services.cosmos_sdk import create_azure_cosmos_repositories
    from app.services.purchase_writes import PurchaseWriteBuilder

    environment = os.environ if environment is None else environment
    _, write_repository, close_client = create_azure_cosmos_repositories(
        endpoint=environment["NESTOCK_COSMOS_ENDPOINT"],
        database_name=environment["NESTOCK_COSMOS_DATABASE"],
        container_name=environment["NESTOCK_COSMOS_CONTAINER"],
    )
    atexit.register(close_client)
    return ApprovalService(write_repository, PurchaseWriteBuilder(read_repository))
