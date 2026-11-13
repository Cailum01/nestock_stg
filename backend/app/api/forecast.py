"""B2 read-only forecast routes. Not registered here; app/main.py includes `router`.

Routes and Agent tools share ForecastService. Responses are estimates and never
change confirmed inventory. The history source sits behind `get_transaction_history`
so the Cosmos adapter (or a later lake adapter) replaces the demo without changing
these routes.

`Transaction.created_at` is read as event time per Issue #113; when no event date
is provided, the first confirmation time (`decided_at`) is used. Forecasting is
enabled, while individual items without enough history still return
`insufficient_data` (see "Time semantics" in services/forecast.py).
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


# Defined before "/{item_id}" so "reorder" is never read as an item id.
@router.get("/reorder", response_model=list[ItemForecast])
async def reorder_candidates(
    service: Annotated[ForecastService, Depends(get_forecast_service)],
    clock: Annotated[Callable[[], datetime], Depends(get_clock)],
) -> list[ItemForecast]:
    """Items worth restocking soon, soonest first. Empty when none qualify.

    Same selection the Agent should reuse (`ForecastService.reorder_candidates`).
    Estimates only: nothing here changes confirmed inventory or the shopping list.
    """
    summary = await service.reorder_candidates(settings.demo_household_id, clock())
    return summary.candidates


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
