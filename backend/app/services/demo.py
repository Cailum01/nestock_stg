"""Read-only fixtures. No store, database client, or persistent mutation."""

from datetime import UTC, datetime

from app.core.config import settings
from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus

DEMO_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class DemoInventoryService:
    async def list_items(self, household_id: str) -> list[InventoryItem]:
        if household_id != settings.demo_household_id:
            return []
        return [
            InventoryItem(
                item_id="demo-rice",
                household_id=household_id,
                name="白米",
                quantity=2,
                unit="包",
                location="廚房櫃子",
                created_at=DEMO_TIME,
                updated_at=DEMO_TIME,
            )
        ]


class DemoShoppingService:
    async def list_items(self, household_id: str) -> list[ShoppingItem]:
        if household_id != settings.demo_household_id:
            return []
        return [
            ShoppingItem(
                shopping_item_id="demo-milk",
                household_id=household_id,
                name="牛奶",
                quantity=1,
                unit="瓶",
                status=ShoppingStatus.PENDING,
                created_at=DEMO_TIME,
                updated_at=DEMO_TIME,
            )
        ]
