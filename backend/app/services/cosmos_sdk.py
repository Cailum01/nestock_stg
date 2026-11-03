"""Azure Cosmos SDK adapter for the B1 read-only repository."""

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Callable, Protocol

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import DefaultAzureCredential

from app.services.cosmos_read import CosmosReadRepository

logger = logging.getLogger(__name__)


class CosmosContainer(Protocol):
    def query_items(
        self,
        *,
        query: str,
        parameters: Sequence[Mapping[str, object]],
        partition_key: str,
        response_hook: Callable[[Mapping[str, str], Mapping[str, object]], None],
    ) -> Iterable[Mapping[str, object]]: ...

    def read_item(
        self,
        *,
        item: str,
        partition_key: str,
        response_hook: Callable[[Mapping[str, str], Mapping[str, object]], None],
    ) -> Mapping[str, object]: ...


class AzureCosmosDocumentQueryClient:
    """Run household-partitioned SDK queries without blocking the event loop."""

    def __init__(self, container: CosmosContainer) -> None:
        self._container = container

    async def query_items(
        self,
        *,
        query: str,
        parameters: Sequence[Mapping[str, object]],
        partition_key: str,
    ) -> Sequence[Mapping[str, object]]:
        return await asyncio.to_thread(
            self._query_items,
            query=query,
            parameters=parameters,
            partition_key=partition_key,
        )

    def _query_items(
        self,
        *,
        query: str,
        parameters: Sequence[Mapping[str, object]],
        partition_key: str,
    ) -> list[Mapping[str, object]]:
        request_charges: list[float] = []
        capture_response = self._request_charge_callback(request_charges)
        started_at = time.perf_counter()
        documents = self._container.query_items(
            query=query,
            parameters=[dict(parameter) for parameter in parameters],
            partition_key=partition_key,
            response_hook=capture_response,
        )
        results = [dict(document) for document in documents]
        self._log_read_metrics(
            operation="query",
            request_charges=request_charges,
            result_count=len(results),
            started_at=started_at,
        )
        return results

    async def read_item(
        self,
        *,
        item_id: str,
        partition_key: str,
    ) -> Mapping[str, object] | None:
        return await asyncio.to_thread(
            self._read_item,
            item_id=item_id,
            partition_key=partition_key,
        )

    def _read_item(
        self,
        *,
        item_id: str,
        partition_key: str,
    ) -> Mapping[str, object] | None:
        request_charges: list[float] = []
        started_at = time.perf_counter()
        try:
            document = self._container.read_item(
                item=item_id,
                partition_key=partition_key,
                response_hook=self._request_charge_callback(request_charges),
            )
        except CosmosResourceNotFoundError:
            self._log_read_metrics(
                operation="point_read",
                request_charges=request_charges,
                result_count=0,
                started_at=started_at,
            )
            return None

        self._log_read_metrics(
            operation="point_read",
            request_charges=request_charges,
            result_count=1,
            started_at=started_at,
        )
        return dict(document)

    @staticmethod
    def _request_charge_callback(
        request_charges: list[float],
    ) -> Callable[[Mapping[str, str], Mapping[str, object]], None]:
        def capture_response(
            headers: Mapping[str, str],
            response: Mapping[str, object],
        ) -> None:
            del response
            charge_header = next(
                (value for key, value in headers.items() if key.lower() == "x-ms-request-charge"),
                None,
            )
            if charge_header is not None:
                request_charges.append(float(charge_header))

        return capture_response

    @staticmethod
    def _log_read_metrics(
        *,
        operation: str,
        request_charges: Sequence[float],
        result_count: int,
        started_at: float,
    ) -> None:
        total_request_charge = sum(request_charges) if request_charges else None
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "Cosmos read %s completed: request_charge=%s result_count=%d duration_ms=%.1f",
            operation,
            total_request_charge,
            result_count,
            duration_ms,
        )


def create_azure_cosmos_read_repository(
    *,
    endpoint: str,
    database_name: str,
    container_name: str,
) -> tuple[CosmosReadRepository, Callable[[], None]]:
    """Create a read repository using Azure CLI, managed identity, or workload identity."""

    if not endpoint.startswith("https://"):
        raise ValueError("Cosmos endpoint must use HTTPS.")
    if not database_name or not container_name:
        raise ValueError("Cosmos database and container names must not be empty.")

    credential = DefaultAzureCredential()
    client = CosmosClient(endpoint, credential=credential)
    container = client.get_database_client(database_name).get_container_client(container_name)
    repository = CosmosReadRepository(AzureCosmosDocumentQueryClient(container))

    def close() -> None:
        try:
            client.close()
        finally:
            credential.close()

    return repository, close
