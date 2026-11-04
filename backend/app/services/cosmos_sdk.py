"""Azure Cosmos SDK adapter for the B1 read-only repository."""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from typing import Callable, Protocol

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

from app.services.cosmos_read import CosmosReadRepository


class CosmosContainer(Protocol):
    def query_items(
        self,
        *,
        query: str,
        parameters: Sequence[Mapping[str, object]],
        partition_key: str,
    ) -> Iterable[Mapping[str, object]]: ...


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
        documents = self._container.query_items(
            query=query,
            parameters=[dict(parameter) for parameter in parameters],
            partition_key=partition_key,
        )
        return [dict(document) for document in documents]


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
