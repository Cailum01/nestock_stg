from app.services.demo import DemoInventoryService, DemoShoppingService
from app.services.interfaces import InventoryService, ShoppingService

inventory_service: InventoryService = DemoInventoryService()
shopping_service: ShoppingService = DemoShoppingService()


def get_inventory_service() -> InventoryService:
    return inventory_service


def get_shopping_service() -> ShoppingService:
    return shopping_service
