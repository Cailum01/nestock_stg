"""B2 TransactionHistory over B1's read-only Cosmos repository. No writes.

B1's `CosmosReadRepository.list_transactions` takes a `TransactionQuery`; B2's
port takes a household. This adapter bridges the two and keeps B1's ordering
(newest first, then transaction_id). The whole household history is read: it is
small (about 700-3,000 records a year) and the "still enough?" path may need a
purchase older than the forecast windows.

It reads the same `NESTOCK_COSMOS_*` settings as B1's inventory adapter. Without
them it falls back to the demo history and logs how to enable Cosmos. B1 owns
`app/core/dependencies.py`; until B1 shares its repository there, this opens a
second read-only client to the same container.

Open gaps:
- Feature-only Transactions and links wait for D-6; B1's query reads `item_id` only.
"""

import atexit
import logging
import os
from collections.abc import Mapping
from typing import Protocol

from app.models.domain import Transaction
from app.services.cosmos_read import TransactionQuery
from app.services.interfaces import TransactionHistory
from app.services.transaction_history import DemoTransactionHistory

logger = logging.getLogger(__name__)

COSMOS_SETTINGS = (
    "NESTOCK_COSMOS_ENDPOINT",
    "NESTOCK_COSMOS_DATABASE",
    "NESTOCK_COSMOS_CONTAINER",
)

SETUP_HINT = (
    "Cosmos is not configured, so forecasts use demo transaction history. "
    "To read Cosmos: run `az login` with the Nestock account and select the Nestock "
    "subscription, then set NESTOCK_COSMOS_ENDPOINT, NESTOCK_COSMOS_DATABASE and "
    "NESTOCK_COSMOS_CONTAINER (see datalake/azure-database-user-guide.md) and restart "
    "the backend."
)


class TransactionQueryReader(Protocol):
    """The part of B1's CosmosReadRepository this adapter uses."""

    async def list_transactions(self, query: TransactionQuery) -> list[Transaction]: ...


class CosmosTransactionHistory:
    def __init__(self, repository: TransactionQueryReader) -> None:
        self._repository = repository

    async def list_transactions(self, household_id: str) -> list[Transaction]:
        return await self._repository.list_transactions(TransactionQuery(household_id=household_id))


def create_transaction_history(
    environment: Mapping[str, str] | None = None,
) -> TransactionHistory:
    """Cosmos when all three settings are present, demo history when none are."""
    environment = os.environ if environment is None else environment
    configured = {name: environment[name] for name in COSMOS_SETTINGS if name in environment}
    if not configured:
        logger.warning(SETUP_HINT)
        return DemoTransactionHistory()
    missing = [name for name in COSMOS_SETTINGS if name not in configured]
    if missing:
        raise RuntimeError(
            "Cosmos read configuration is incomplete; missing " + ", ".join(missing) + "."
        )
    if any(not value for value in configured.values()):
        raise RuntimeError("Cosmos read configuration values must not be empty.")

    # Imported here so the demo path never loads the Azure SDK.
    from app.services.cosmos_sdk import create_azure_cosmos_read_repository

    repository, close_client = create_azure_cosmos_read_repository(
        endpoint=configured["NESTOCK_COSMOS_ENDPOINT"],
        database_name=configured["NESTOCK_COSMOS_DATABASE"],
        container_name=configured["NESTOCK_COSMOS_CONTAINER"],
    )
    atexit.register(close_client)
    return CosmosTransactionHistory(repository)
