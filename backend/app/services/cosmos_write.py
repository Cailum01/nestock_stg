"""Offline-testable Cosmos transactional write primitives.

The domain service must validate the operation and approval before calling this
repository. This module is not wired into the application or a Cosmos SDK.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class BatchOperationConflict(Exception):
    """A transactional batch failed at the indicated zero-based operation."""

    def __init__(self, operation_index: int) -> None:
        super().__init__(f"Transactional batch conflict at operation {operation_index}.")
        self.operation_index = operation_index


class OperationIdConflict(Exception):
    """An operation ID was reused with a different canonical payload."""


class WriteStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


@dataclass(frozen=True)
class CreateDocument:
    document: Mapping[str, object]


@dataclass(frozen=True)
class ReplaceDocument:
    document: Mapping[str, object]
    if_match_etag: str


BatchOperation = CreateDocument | ReplaceDocument


class TransactionalBatchClient(Protocol):
    """Port for a client that executes operations atomically per partition."""

    async def execute_batch(
        self,
        *,
        partition_key: str,
        operations: Sequence[BatchOperation],
    ) -> None: ...

    async def read_item(
        self,
        *,
        item_id: str,
        partition_key: str,
    ) -> Mapping[str, object] | None: ...


def _operation_receipt_id(operation_id: str) -> str:
    operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"operation:{operation_key}"


def _canonical_fingerprint(
    operations: Sequence[BatchOperation],
) -> str:
    canonical_operations: list[dict[str, object]] = []
    for operation in operations:
        if isinstance(operation, CreateDocument):
            canonical_operations.append({"kind": "create", "document": dict(operation.document)})
        else:
            canonical_operations.append({"kind": "replace", "document": dict(operation.document)})
    try:
        canonical_payload = json.dumps(
            canonical_operations,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Batch operations must contain canonical JSON values.") from error
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


class CosmosWriteRepository:
    """Execute same-household writes with durable operation-ID idempotency."""

    def __init__(self, client: TransactionalBatchClient) -> None:
        self._client = client

    async def execute_idempotent_batch(
        self,
        *,
        household_id: str,
        operation_id: str,
        operations: Sequence[BatchOperation],
    ) -> WriteStatus:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if not operation_id:
            raise ValueError("operation_id must not be empty.")
        if not operations:
            raise ValueError("A write batch must contain at least one operation.")

        for operation in operations:
            document = operation.document
            if document.get("household_id") != household_id:
                raise ValueError("Every batch document must match the household partition.")
            if not isinstance(document.get("id"), str) or not document["id"]:
                raise ValueError("Every batch document must have a non-empty Cosmos id.")
            if document.get("record_type") == "operation_receipt":
                raise ValueError("Operation receipts are managed by this repository.")
            if isinstance(operation, ReplaceDocument) and not operation.if_match_etag:
                raise ValueError("Replace operations require the current Cosmos _etag.")

        fingerprint = _canonical_fingerprint(operations)
        receipt = {
            "id": _operation_receipt_id(operation_id),
            "record_type": "operation_receipt",
            "household_id": household_id,
            "operation_id": operation_id,
            "request_fingerprint": fingerprint,
        }
        try:
            await self._client.execute_batch(
                partition_key=household_id,
                operations=(CreateDocument(receipt), *operations),
            )
        except BatchOperationConflict as conflict:
            if conflict.operation_index != 0:
                raise
            existing_receipt = await self._client.read_item(
                item_id=receipt["id"],
                partition_key=household_id,
            )
            if existing_receipt is None:
                raise
            if (
                existing_receipt.get("record_type") != "operation_receipt"
                or existing_receipt.get("household_id") != household_id
                or existing_receipt.get("operation_id") != operation_id
            ):
                raise ValueError("Stored operation receipt does not match its partition key.")
            if existing_receipt.get("request_fingerprint") != fingerprint:
                raise OperationIdConflict(
                    "operation_id was already used with different canonical values."
                ) from conflict
            return WriteStatus.ALREADY_APPLIED

        return WriteStatus.APPLIED
