"""Provisional read ports. Mutation ports require owner-agreed approval semantics.

No public write method exists until backend validation, approval binding and
durable idempotency are designed together. Forecasting has no inventory write port.
"""

from typing import Protocol

from app.models.domain import InventoryItem, ShoppingItem, Transaction


class InventoryService(Protocol):
    async def list_items(self, household_id: str) -> list[InventoryItem]: ...


class ShoppingService(Protocol):
    async def list_items(self, household_id: str) -> list[ShoppingItem]: ...


class TransactionHistory(Protocol):
    async def list_transactions(self, household_id: str) -> list[Transaction]: ...
