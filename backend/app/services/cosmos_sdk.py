"""Azure Cosmos SDK clients for B1 repositories."""

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Callable, Protocol

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError
from azure.identity import DefaultAzureCredential

from app.services.cosmos_read import CosmosReadRepository
from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    ReplaceDocument,
    TransactionalBatchClient,
)

logger = logging.getLogger(__name__)
COSMOS_TRANSACTIONAL_BATCH_LIMIT = 100
COSMOS_SYSTEM_PROPERTIES = {"_attachments", "_etag", "_rid", "_self", "_ts"}


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


class CosmosTransactionalContainer(CosmosContainer, Protocol):
    def execute_item_batch(
        self,
        batch_operations: Sequence[
            tuple[str, tuple[object, ...]] | tuple[str, tuple[object, ...], Mapping[str, object]]
        ],
        partition_key: str,
        *,
        response_hook: Callable[
            [Mapping[str, str], list[Mapping[str, object]]],
            None,
        ],
    ) -> Sequence[Mapping[str, object]]: ...


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
        except CosmosResourceNotFoundError as error:
            if not request_charges:
                self._append_request_charge(error.headers, request_charges)
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
            AzureCosmosDocumentQueryClient._append_request_charge(headers, request_charges)

        return capture_response

    @staticmethod
    def _append_request_charge(
        headers: Mapping[str, str],
        request_charges: list[float],
    ) -> None:
        charge_header = next(
            (value for key, value in headers.items() if key.lower() == "x-ms-request-charge"),
            None,
        )
        if charge_header is not None:
            request_charges.append(float(charge_header))

    @staticmethod
    def _log_read_metrics(
        *,
        operation: str,
        request_charges: Sequence[float],
        result_count: int,
        started_at: float,
    ) -> None:
        total_request_charge = str(sum(request_charges)) if request_charges else "unavailable"
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "Cosmos read %s completed: request_charge=%s result_count=%d duration_ms=%.1f",
            operation,
            total_request_charge,
            result_count,
            duration_ms,
        )


class AzureCosmosTransactionalBatchClient(TransactionalBatchClient):
    """Run household-partitioned SDK batches without blocking the event loop."""

    def __init__(self, container: CosmosTransactionalContainer) -> None:
        self._container = container
        self._read_client = AzureCosmosDocumentQueryClient(container)

    async def execute_batch(
        self,
        *,
        partition_key: str,
        operations: Sequence[CreateDocument | ReplaceDocument],
    ) -> None:
        if not partition_key:
            raise ValueError("partition_key must not be empty.")
        if not operations:
            raise ValueError("A transactional batch must contain at least one operation.")
        if len(operations) > COSMOS_TRANSACTIONAL_BATCH_LIMIT:
            raise ValueError(
                f"Cosmos transactional batches cannot exceed "
                f"{COSMOS_TRANSACTIONAL_BATCH_LIMIT} operations."
            )

        sdk_operations = [
            self._to_sdk_operation(partition_key, operation) for operation in operations
        ]
        await asyncio.to_thread(
            self._execute_batch,
            partition_key=partition_key,
            operations=sdk_operations,
        )

    async def read_item(
        self,
        *,
        item_id: str,
        partition_key: str,
    ) -> Mapping[str, object] | None:
        return await self._read_client.read_item(
            item_id=item_id,
            partition_key=partition_key,
        )

    def _execute_batch(
        self,
        *,
        partition_key: str,
        operations: Sequence[
            tuple[str, tuple[object, ...]] | tuple[str, tuple[object, ...], Mapping[str, object]]
        ],
    ) -> None:
        request_charges: list[float] = []
        started_at = time.perf_counter()

        def capture_response(
            headers: Mapping[str, str],
            response: list[Mapping[str, object]],
        ) -> None:
            del response
            AzureCosmosDocumentQueryClient._append_request_charge(headers, request_charges)

        try:
            self._container.execute_item_batch(
                batch_operations=operations,
                partition_key=partition_key,
                response_hook=capture_response,
            )
        except CosmosBatchOperationError as error:
            if not request_charges and isinstance(error.headers, Mapping):
                AzureCosmosDocumentQueryClient._append_request_charge(
                    error.headers,
                    request_charges,
                )
            total_request_charge = str(sum(request_charges)) if request_charges else "unavailable"
            logger.warning(
                "Cosmos transactional batch failed: request_charge=%s operation_count=%d "
                "operation_index=%s status_code=%s duration_ms=%.1f",
                total_request_charge,
                len(operations),
                error.error_index,
                error.status_code,
                (time.perf_counter() - started_at) * 1000,
            )
            if (
                error.status_code in {409, 412}
                and isinstance(error.error_index, int)
                and not isinstance(error.error_index, bool)
            ):
                raise BatchOperationConflict(error.error_index) from error
            raise

        total_request_charge = str(sum(request_charges)) if request_charges else "unavailable"
        logger.info(
            "Cosmos transactional batch completed: request_charge=%s operation_count=%d "
            "duration_ms=%.1f",
            total_request_charge,
            len(operations),
            (time.perf_counter() - started_at) * 1000,
        )

    @staticmethod
    def _to_sdk_operation(
        partition_key: str,
        operation: CreateDocument | ReplaceDocument,
    ) -> tuple[str, tuple[object, ...]] | tuple[str, tuple[object, ...], Mapping[str, object]]:
        document = dict(operation.document)
        if document.get("household_id") != partition_key:
            raise ValueError("Every batch document must match the household partition.")
        document_id = document.get("id")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("Every batch document must have a non-empty Cosmos id.")
        body = {
            key: value for key, value in document.items() if key not in COSMOS_SYSTEM_PROPERTIES
        }
        if isinstance(operation, CreateDocument):
            return "create", (body,)
        if not operation.if_match_etag:
            raise ValueError("Replace operations require the current Cosmos _etag.")
        return (
            "replace",
            (document_id, body),
            {"if_match_etag": operation.if_match_etag},
        )


def create_azure_cosmos_read_repository(
    *,
    endpoint: str,
    database_name: str,
    container_name: str,
) -> tuple[CosmosReadRepository, Callable[[], None]]:
    """Create a read repository using Azure CLI, managed identity, or workload identity."""
    container, close = _create_azure_cosmos_container(
        endpoint=endpoint,
        database_name=database_name,
        container_name=container_name,
    )
    return CosmosReadRepository(AzureCosmosDocumentQueryClient(container)), close


def create_azure_cosmos_repositories(
    *,
    endpoint: str,
    database_name: str,
    container_name: str,
) -> tuple[
    CosmosReadRepository,
    CosmosWriteRepository,
    AzureCosmosDocumentQueryClient,
    Callable[[], None],
]:
    """Create read/write repositories and a raw document reader on one SDK client.

    The raw reader returns stored documents unchanged (including storage-only
    metadata), for write planners that replace whole documents.
    """
    container, close = _create_azure_cosmos_container(
        endpoint=endpoint,
        database_name=database_name,
        container_name=container_name,
    )
    read_repository = CosmosReadRepository(AzureCosmosDocumentQueryClient(container))
    write_repository = CosmosWriteRepository(
        AzureCosmosTransactionalBatchClient(container),
        query_client=AzureCosmosDocumentQueryClient(container),
    )
    document_reader = AzureCosmosDocumentQueryClient(container)
    return read_repository, write_repository, document_reader, close


def _create_azure_cosmos_container(
    *,
    endpoint: str,
    database_name: str,
    container_name: str,
) -> tuple[CosmosTransactionalContainer, Callable[[], None]]:
    if not endpoint.startswith("https://"):
        raise ValueError("Cosmos endpoint must use HTTPS.")
    if not database_name or not container_name:
        raise ValueError("Cosmos database and container names must not be empty.")

    credential = DefaultAzureCredential()
    client = CosmosClient(endpoint, credential=credential)
    container = client.get_database_client(database_name).get_container_client(container_name)

    def close() -> None:
        try:
            client.close()
        finally:
            credential.close()

    return container, close
