"""Offline-testable Cosmos document mapping and read repository.

This module deliberately has no Azure SDK dependency and is not wired into the
application. It prepares the B1 read boundary without enabling persistence.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, Protocol, TypeVar

from app.models.domain import InventoryItem, ShoppingItem, Transaction

DomainRecord = InventoryItem | ShoppingItem | Transaction
RecordT = TypeVar("RecordT", InventoryItem, ShoppingItem, Transaction)

_RECORD_METADATA = frozenset({"id", "record_type", "_rid", "_self", "_etag", "_attachments", "_ts"})
_RECORD_IDENTITIES: dict[type[DomainRecord], tuple[str, str]] = {
    InventoryItem: ("inventory_item", "item_id"),
    ShoppingItem: ("shopping_item", "shopping_item_id"),
    Transaction: ("transaction", "transaction_id"),
}
_COSMOS_SYSTEM_FIELDS = frozenset({"_rid", "_self", "_etag", "_attachments", "_ts"})


@dataclass(frozen=True)
class MappedDocument(Generic[RecordT]):
    record: RecordT
    document_id: str
    etag: str | None


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Persisted timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _expected_id(record_type: str, public_id: str) -> str:
    return f"{record_type}:{public_id}"


class CosmosDocumentMapper:
    """Map approved Pydantic domain records to namespaced Cosmos documents."""

    @staticmethod
    def to_document(record: DomainRecord) -> dict[str, object]:
        record_type, identity_field = _RECORD_IDENTITIES[type(record)]
        public_id = getattr(record, identity_field)
        document = record.model_dump(mode="json")
        document["id"] = _expected_id(record_type, public_id)
        document["record_type"] = record_type

        for timestamp_field in ("created_at", "updated_at"):
            timestamp = getattr(record, timestamp_field, None)
            if timestamp is not None:
                document[timestamp_field] = _utc_isoformat(timestamp)

        return document

    @staticmethod
    def from_document(
        model_type: type[RecordT],
        document: Mapping[str, object],
        *,
        household_id: str,
    ) -> RecordT:
        return CosmosDocumentMapper.from_document_with_metadata(
            model_type,
            document,
            household_id=household_id,
        ).record

    @staticmethod
    def from_document_with_metadata(
        model_type: type[RecordT],
        document: Mapping[str, object],
        *,
        household_id: str,
    ) -> MappedDocument[RecordT]:
        expected_record_type, identity_field = _RECORD_IDENTITIES[model_type]
        if document.get("record_type") != expected_record_type:
            raise ValueError(
                f"Expected record_type {expected_record_type!r}, "
                f"got {document.get('record_type')!r}."
            )
        if document.get("household_id") != household_id:
            raise ValueError("Document household_id does not match the query scope.")

        public_id = document.get(identity_field)
        expected_document_id = _expected_id(expected_record_type, str(public_id))
        if document.get("id") != expected_document_id:
            raise ValueError("Document id does not match its namespaced domain ID.")

        domain_data = {
            key: value
            for key, value in document.items()
            if key not in _RECORD_METADATA and key not in _COSMOS_SYSTEM_FIELDS
        }
        etag = document.get("_etag")
        if etag is not None and not isinstance(etag, str):
            raise ValueError("Cosmos _etag must be a string when present.")
        return MappedDocument(
            record=model_type.model_validate(domain_data),
            document_id=expected_document_id,
            etag=etag,
        )


@dataclass(frozen=True)
class TransactionQuery:
    """B1's currently approved raw Transaction filters.

    Feature-keyword lookup and linked-transaction attribution are intentionally
    absent until the shared Transaction/link contracts are approved.
    """

    household_id: str
    item_id: str | None = None
    since: datetime | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        if not self.household_id:
            raise ValueError("household_id must not be empty.")
        if self.item_id is not None and not self.item_id:
            raise ValueError("item_id must be non-empty when provided.")
        if self.since is not None:
            _utc_isoformat(self.since)
        if self.limit is not None and (
            isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0
        ):
            raise ValueError("limit must be a positive integer.")


@dataclass(frozen=True)
class CosmosQuery:
    query: str
    parameters: tuple[dict[str, object], ...]
    partition_key: str


def build_transaction_query(spec: TransactionQuery) -> CosmosQuery:
    """Build a parameterized single-household query with stable ordering."""

    select = "SELECT * FROM c"
    if spec.limit is not None:
        select = "SELECT TOP @limit * FROM c"

    clauses = [
        "c.record_type = @record_type",
        "c.household_id = @household_id",
    ]
    parameters: list[dict[str, object]] = [
        {"name": "@record_type", "value": "transaction"},
        {"name": "@household_id", "value": spec.household_id},
    ]
    if spec.item_id is not None:
        clauses.append("c.item_id = @item_id")
        parameters.append({"name": "@item_id", "value": spec.item_id})
    if spec.since is not None:
        clauses.append("c.created_at >= @since")
        parameters.append({"name": "@since", "value": _utc_isoformat(spec.since)})
    if spec.limit is not None:
        parameters.append({"name": "@limit", "value": spec.limit})

    query = (
        f"{select} WHERE {' AND '.join(clauses)} ORDER BY c.created_at DESC, c.transaction_id ASC"
    )
    return CosmosQuery(
        query=query,
        parameters=tuple(parameters),
        partition_key=spec.household_id,
    )


class DocumentQueryClient(Protocol):
    """Household-scoped async read port implemented by the Cosmos SDK adapter."""

    async def query_items(
        self,
        *,
        query: str,
        parameters: Sequence[Mapping[str, object]],
        partition_key: str,
    ) -> Sequence[Mapping[str, object]]: ...

    async def read_item(
        self,
        *,
        item_id: str,
        partition_key: str,
    ) -> Mapping[str, object] | None: ...


class CosmosReadRepository:
    """Read-only repository over one household-data container query port."""

    def __init__(self, client: DocumentQueryClient) -> None:
        self._client = client

    async def list_inventory(self, household_id: str) -> list[InventoryItem]:
        return await self._list_records(
            InventoryItem,
            record_type="inventory_item",
            household_id=household_id,
        )

    async def list_shopping(self, household_id: str) -> list[ShoppingItem]:
        return await self._list_records(
            ShoppingItem,
            record_type="shopping_item",
            household_id=household_id,
        )

    async def read_inventory_item(
        self,
        *,
        household_id: str,
        item_id: str,
    ) -> InventoryItem | None:
        return await self._read_record(
            InventoryItem,
            record_type="inventory_item",
            identity=item_id,
            identity_field="item_id",
            household_id=household_id,
        )

    async def read_shopping_item(
        self,
        *,
        household_id: str,
        shopping_item_id: str,
    ) -> ShoppingItem | None:
        return await self._read_record(
            ShoppingItem,
            record_type="shopping_item",
            identity=shopping_item_id,
            identity_field="shopping_item_id",
            household_id=household_id,
        )

    async def list_transactions(self, query: TransactionQuery) -> list[Transaction]:
        cosmos_query = build_transaction_query(query)
        documents = await self._client.query_items(
            query=cosmos_query.query,
            parameters=cosmos_query.parameters,
            partition_key=cosmos_query.partition_key,
        )
        return [
            CosmosDocumentMapper.from_document(
                Transaction,
                document,
                household_id=query.household_id,
            )
            for document in documents
        ]

    async def _read_record(
        self,
        model_type: type[RecordT],
        *,
        record_type: str,
        identity: str,
        identity_field: str,
        household_id: str,
    ) -> RecordT | None:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if not identity:
            raise ValueError(f"{identity_field} must not be empty.")
        document = await self._client.read_item(
            item_id=_expected_id(record_type, identity),
            partition_key=household_id,
        )
        if document is None:
            return None
        return CosmosDocumentMapper.from_document(
            model_type,
            document,
            household_id=household_id,
        )

    async def _list_records(
        self,
        model_type: type[RecordT],
        *,
        record_type: str,
        household_id: str,
    ) -> list[RecordT]:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        query = CosmosQuery(
            query=(
                "SELECT * FROM c WHERE c.record_type = @record_type "
                "AND c.household_id = @household_id"
            ),
            parameters=(
                {"name": "@record_type", "value": record_type},
                {"name": "@household_id", "value": household_id},
            ),
            partition_key=household_id,
        )
        documents = await self._client.query_items(
            query=query.query,
            parameters=query.parameters,
            partition_key=query.partition_key,
        )
        return [
            CosmosDocumentMapper.from_document(
                model_type,
                document,
                household_id=household_id,
            )
            for document in documents
        ]


class CosmosInventoryService:
    """Adapt the B1 repository to the shared InventoryService read port."""

    def __init__(self, repository: CosmosReadRepository) -> None:
        self._repository = repository

    async def list_items(self, household_id: str) -> list[InventoryItem]:
        return await self._repository.list_inventory(household_id)


class CosmosShoppingService:
    """Adapt the B1 repository to the shared ShoppingService read port."""

    def __init__(self, repository: CosmosReadRepository) -> None:
        self._repository = repository

    async def list_items(self, household_id: str) -> list[ShoppingItem]:
        return await self._repository.list_shopping(household_id)
