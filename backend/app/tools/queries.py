from dataclasses import dataclass

from app.models.domain import InventoryItem, ShoppingItem
from app.services.interfaces import InventoryService, ShoppingService


@dataclass(frozen=True)
class QueryTools:
    """Inject the same services as API routes. A1 owns registry integration."""

    inventory: InventoryService
    shopping: ShoppingService

    async def list_inventory(self, household_id: str) -> list[InventoryItem]:
        return await self.inventory.list_items(household_id)

    async def list_shopping(self, household_id: str) -> list[ShoppingItem]:
        return await self.shopping.list_items(household_id)
