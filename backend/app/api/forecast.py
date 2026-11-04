"""B2 read-only forecast routes. Not registered here; app/main.py includes `router`.

Routes and Agent tools share ForecastService. Responses are estimates and never
change confirmed inventory. The history source sits behind `get_transaction_history`
so the Cosmos adapter (or a later lake adapter) replaces the demo without changing
these routes.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.core.dependencies import get_inventory_service
from app.models.forecast import ItemForecast
from app.services.cosmos_transaction_history import create_transaction_history
from app.services.forecast import ForecastService, ItemNotFoundError
from app.services.interfaces import InventoryService, TransactionHistory

router = APIRouter(prefix="/forecast", tags=["forecast"])

# Cosmos when NESTOCK_COSMOS_* are set, otherwise demo history with a setup hint.
transaction_history: TransactionHistory = create_transaction_history()


def get_transaction_history() -> TransactionHistory:
    return transaction_history


def get_clock() -> Callable[[], datetime]:
    return lambda: datetime.now(UTC)


def get_forecast_service(
    inventory: Annotated[InventoryService, Depends(get_inventory_service)],
    history: Annotated[TransactionHistory, Depends(get_transaction_history)],
) -> ForecastService:
    return ForecastService(inventory, history)


@router.get("", response_model=list[ItemForecast])
async def household_forecast(
    service: Annotated[ForecastService, Depends(get_forecast_service)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> list[ItemForecast]:
    return await service.forecast_household(settings.demo_household_id, clock())


@router.get(
    "/{item_id}",
    response_model=ItemForecast,
    responses={404: {"description": "Item not in the household's active inventory"}},
)
async def item_forecast(
    item_id: str,
    service: Annotated[ForecastService, Depends(get_forecast_service)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> ItemForecast:
    try:
        return await service.forecast_item(settings.demo_household_id, item_id, clock())
    except ItemNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from exc
