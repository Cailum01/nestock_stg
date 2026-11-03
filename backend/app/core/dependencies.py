from app.services.demo import DemoInventoryService, DemoShoppingService
from app.services.interfaces import InventoryService, ShoppingService
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.speech import MockSpeechToTextService, SpeechToTextService

inventory_service: InventoryService = DemoInventoryService()
shopping_service: ShoppingService = DemoShoppingService()
media_storage_service: MediaStorageService = LocalStagingMediaStorage()
speech_service: SpeechToTextService = MockSpeechToTextService()


def get_inventory_service() -> InventoryService:
    return inventory_service


def get_shopping_service() -> ShoppingService:
    return shopping_service


def get_media_storage_service() -> MediaStorageService:
    return media_storage_service


def get_speech_service() -> SpeechToTextService:
    return speech_service
