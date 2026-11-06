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


class OperationReceiptInconsistency(Exception):
    """Only part of an expected multi-operation receipt set exists."""


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


@dataclass(frozen=True)
class OperationReceipt:
    household_id: str
    operation_id: str
    request_fingerprint: str
    batch_fingerprint: str


@dataclass(frozen=True)
class PendingApprovalRecord:
    household_id: str
    approval_id: str
    document: Mapping[str, object]
    etag: str


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


def _pending_approval_id(approval_id: str) -> str:
    return f"pending_approval:{approval_id}"


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


def _batch_fingerprint(
    operation_fingerprints: Mapping[str, str],
    shared_operations_fingerprint: str | None,
) -> str:
    canonical_payload = json.dumps(
        {
            "operations": [
                {"operation_id": operation_id, "request_fingerprint": fingerprint}
                for operation_id, fingerprint in sorted(operation_fingerprints.items())
            ],
            "shared_operations_fingerprint": shared_operations_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


class CosmosWriteRepository:
    """Execute same-household writes with durable operation-ID idempotency."""

    def __init__(self, client: TransactionalBatchClient) -> None:
        self._client = client

    async def read_pending_approval(
        self,
        *,
        household_id: str,
        approval_id: str,
    ) -> PendingApprovalRecord | None:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if not approval_id:
            raise ValueError("approval_id must not be empty.")

        document_id = _pending_approval_id(approval_id)
        document = await self._client.read_item(
            item_id=document_id,
            partition_key=household_id,
        )
        if document is None:
            return None
        if (
            document.get("id") != document_id
            or document.get("record_type") != "pending_approval"
            or document.get("household_id") != household_id
            or document.get("approval_id") != approval_id
        ):
            raise ValueError("Stored pending approval does not match its partition key.")
        etag = document.get("_etag")
        if not isinstance(etag, str) or not etag:
            raise ValueError("Stored pending approval has no valid Cosmos _etag.")

        return PendingApprovalRecord(
            household_id=household_id,
            approval_id=approval_id,
            document=dict(document),
            etag=etag,
        )

    async def read_operation_receipt(
        self,
        *,
        household_id: str,
        operation_id: str,
    ) -> OperationReceipt | None:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if not operation_id:
            raise ValueError("operation_id must not be empty.")

        receipt_id = _operation_receipt_id(operation_id)
        document = await self._client.read_item(
            item_id=receipt_id,
            partition_key=household_id,
        )
        if document is None:
            return None
        if (
            document.get("id") != receipt_id
            or document.get("record_type") != "operation_receipt"
            or document.get("household_id") != household_id
            or document.get("operation_id") != operation_id
        ):
            raise ValueError("Stored operation receipt does not match its partition key.")
        fingerprint = document.get("request_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("Stored operation receipt has no valid request fingerprint.")
        batch_fingerprint = document.get("batch_fingerprint")
        if (
            not isinstance(batch_fingerprint, str)
            or len(batch_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in batch_fingerprint)
        ):
            raise ValueError("Stored operation receipt has no valid batch fingerprint.")

        return OperationReceipt(
            household_id=household_id,
            operation_id=operation_id,
            request_fingerprint=fingerprint,
            batch_fingerprint=batch_fingerprint,
        )

    async def execute_idempotent_batch(
        self,
        *,
        household_id: str,
        operation_id: str,
        operations: Sequence[BatchOperation],
    ) -> WriteStatus:
        return await self.execute_idempotent_batches(
            household_id=household_id,
            operations_by_id={operation_id: operations},
        )

    async def execute_idempotent_batches(
        self,
        *,
        household_id: str,
        operations_by_id: Mapping[str, Sequence[BatchOperation]],
        shared_operations: Sequence[BatchOperation] = (),
    ) -> WriteStatus:
        """Atomically apply operations, receipts, and optional batch-scoped writes."""
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if not operations_by_id:
            raise ValueError("A write batch must contain at least one operation.")

        receipts: list[dict[str, object]] = []
        batch_operations: list[BatchOperation] = []
        fingerprints: dict[str, str] = {}
        for operation_id, operations in sorted(operations_by_id.items()):
            if not operation_id:
                raise ValueError("operation_id must not be empty.")
            if not operations:
                raise ValueError("Every operation ID must contain at least one batch operation.")
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
            fingerprints[operation_id] = fingerprint
            receipts.append(
                {
                    "id": _operation_receipt_id(operation_id),
                    "record_type": "operation_receipt",
                    "household_id": household_id,
                    "operation_id": operation_id,
                    "request_fingerprint": fingerprint,
                }
            )
            batch_operations.extend(operations)

        for operation in shared_operations:
            document = operation.document
            if document.get("household_id") != household_id:
                raise ValueError("Every batch document must match the household partition.")
            if not isinstance(document.get("id"), str) or not document["id"]:
                raise ValueError("Every batch document must have a non-empty Cosmos id.")
            if document.get("record_type") == "operation_receipt":
                raise ValueError("Operation receipts are managed by this repository.")
            if isinstance(operation, ReplaceDocument) and not operation.if_match_etag:
                raise ValueError("Replace operations require the current Cosmos _etag.")

        shared_operations_fingerprint = (
            _canonical_fingerprint(shared_operations) if shared_operations else None
        )
        batch_fingerprint = _batch_fingerprint(fingerprints, shared_operations_fingerprint)
        for receipt in receipts:
            receipt["batch_fingerprint"] = batch_fingerprint
        batch_operations.extend(shared_operations)

        try:
            await self._client.execute_batch(
                partition_key=household_id,
                operations=(
                    *(CreateDocument(receipt) for receipt in receipts),
                    *batch_operations,
                ),
            )
        except BatchOperationConflict as conflict:
            if conflict.operation_index >= len(receipts):
                raise
            existing_receipts = {
                operation_id: await self.read_operation_receipt(
                    household_id=household_id,
                    operation_id=operation_id,
                )
                for operation_id in fingerprints
            }
            found_receipts = {
                operation_id: receipt
                for operation_id, receipt in existing_receipts.items()
                if receipt is not None
            }
            if not found_receipts:
                raise
            if len(found_receipts) != len(fingerprints):
                raise OperationReceiptInconsistency(
                    "Only part of the batch operation receipts exists; reconciliation is required."
                ) from conflict
            for operation_id, receipt in found_receipts.items():
                if (
                    receipt.request_fingerprint != fingerprints[operation_id]
                    or receipt.batch_fingerprint != batch_fingerprint
                ):
                    raise OperationIdConflict(
                        f"operation_id {operation_id!r} was already used with "
                        "different canonical values or batch membership."
                    ) from conflict
            return WriteStatus.ALREADY_APPLIED

        return WriteStatus.APPLIED
