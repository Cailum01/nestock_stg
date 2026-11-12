"""Offline-testable Cosmos transactional write repository.

The domain service must validate the operation and approval before calling this
repository. Its Azure SDK client lives in ``cosmos_sdk`` and is not wired into
application composition.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.models.domain import ChangeEvent
from app.services.cosmos_read import CosmosDocumentMapper, DocumentQueryClient


class BatchOperationConflict(Exception):
    """A transactional batch failed at the indicated zero-based operation."""

    def __init__(self, operation_index: int) -> None:
        super().__init__(f"Transactional batch conflict at operation {operation_index}.")
        self.operation_index = operation_index


class OperationIdConflict(Exception):
    """An operation ID was reused with a different canonical payload."""


class OperationReceiptInconsistency(Exception):
    """Only part of an expected multi-operation receipt set exists."""


class ApprovalIdConflict(Exception):
    """An approval ID already exists with different canonical values."""


class WriteStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


@dataclass(frozen=True)
class CreateDocument:
    document: Mapping[str, object]


def change_event_create(event: ChangeEvent) -> CreateDocument:
    """Build the append-only change_event create for an operation's batch."""
    if event.event_id != event.operation_id:
        raise ValueError("ChangeEvent.event_id must equal operation_id.")
    return CreateDocument(CosmosDocumentMapper.to_document(event))


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


def _pending_approval_record(
    document: Mapping[str, object],
    *,
    household_id: str,
    approval_id: str,
) -> PendingApprovalRecord:
    if (
        document.get("id") != _pending_approval_id(approval_id)
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


def _without_cosmos_system_properties(document: Mapping[str, object]) -> dict[str, object]:
    system_properties = {"_attachments", "_etag", "_rid", "_self", "_ts"}
    return {key: value for key, value in document.items() if key not in system_properties}


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

    def __init__(
        self,
        client: TransactionalBatchClient,
        *,
        query_client: DocumentQueryClient | None = None,
    ) -> None:
        self._client = client
        self._query_client = query_client

    async def create_pending_approval(
        self,
        *,
        household_id: str,
        approval: Mapping[str, object],
        ttl_seconds: int,
    ) -> PendingApprovalRecord:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer.")

        approval_id = approval.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise ValueError("approval_id must not be empty.")
        document_id = _pending_approval_id(approval_id)
        if (
            approval.get("id") != document_id
            or approval.get("record_type") != "pending_approval"
            or approval.get("household_id") != household_id
            or approval.get("status") != "pending"
        ):
            raise ValueError(
                "Pending approval must have a matching household, ID, and pending status."
            )

        document = _without_cosmos_system_properties(approval)
        document["ttl"] = ttl_seconds
        try:
            await self._client.execute_batch(
                partition_key=household_id,
                operations=(CreateDocument(document),),
            )
        except BatchOperationConflict as conflict:
            if conflict.operation_index != 0:
                raise
            created = await self.read_pending_approval(
                household_id=household_id,
                approval_id=approval_id,
            )
            if created is None:
                raise
            if _without_cosmos_system_properties(created.document) != document:
                raise ApprovalIdConflict(
                    f"approval_id {approval_id!r} already exists with different canonical values."
                ) from conflict
            return created

        created = await self.read_pending_approval(
            household_id=household_id,
            approval_id=approval_id,
        )
        if created is None:
            raise RuntimeError("Created pending approval could not be read back.")
        return created

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
        return _pending_approval_record(
            document,
            household_id=household_id,
            approval_id=approval_id,
        )

    async def list_open_approvals(
        self,
        *,
        household_id: str,
    ) -> list[PendingApprovalRecord]:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if self._query_client is None:
            raise RuntimeError("list_open_approvals requires a household-scoped query client.")

        documents = await self._query_client.query_items(
            query=(
                "SELECT * FROM c WHERE c.record_type = @record_type "
                "AND c.household_id = @household_id "
                "AND c.status IN (@pending_status, @confirmed_status)"
            ),
            parameters=(
                {"name": "@record_type", "value": "pending_approval"},
                {"name": "@household_id", "value": household_id},
                {"name": "@pending_status", "value": "pending"},
                {"name": "@confirmed_status", "value": "confirmed"},
            ),
            partition_key=household_id,
        )
        approvals: list[PendingApprovalRecord] = []
        for document in documents:
            approval_id = document.get("approval_id")
            if not isinstance(approval_id, str) or not approval_id:
                raise ValueError("Stored pending approval has no valid approval_id.")
            if document.get("status") not in {"pending", "confirmed"}:
                raise ValueError("Stored approval query returned a non-open record.")
            approvals.append(
                _pending_approval_record(
                    document,
                    household_id=household_id,
                    approval_id=approval_id,
                )
            )
        return approvals

    async def replace_pending_approval(
        self,
        *,
        household_id: str,
        approval_id: str,
        document: Mapping[str, object],
        if_match_etag: str,
    ) -> PendingApprovalRecord:
        if not household_id:
            raise ValueError("household_id must not be empty.")
        if not approval_id:
            raise ValueError("approval_id must not be empty.")
        if not if_match_etag:
            raise ValueError("if_match_etag must not be empty.")
        if (
            document.get("id") != _pending_approval_id(approval_id)
            or document.get("record_type") != "pending_approval"
            or document.get("household_id") != household_id
            or document.get("approval_id") != approval_id
        ):
            raise ValueError("Replacement approval must match its household and identity.")
        status = document.get("status")
        if not isinstance(status, str) or status not in {"confirmed", "cancelled", "expired"}:
            raise ValueError(
                "Approval-only replacement status must be confirmed, cancelled, or expired."
            )

        current = await self.read_pending_approval(
            household_id=household_id,
            approval_id=approval_id,
        )
        if current is None:
            raise ValueError("Pending approval does not exist.")
        if current.etag != if_match_etag:
            raise BatchOperationConflict(0)
        current_status = current.document.get("status")
        if not isinstance(current_status, str):
            raise ValueError("Stored pending approval has no valid status.")
        allowed_transitions = {
            "pending": {"confirmed", "cancelled", "expired"},
            "confirmed": {"cancelled", "expired"},
        }
        if status not in allowed_transitions.get(current_status, set()):
            raise ValueError(
                f"Approval status cannot transition from {current_status!r} to {status!r} "
                "with an approval-only replacement."
            )

        await self._client.execute_batch(
            partition_key=household_id,
            operations=(
                ReplaceDocument(
                    document=_without_cosmos_system_properties(document),
                    if_match_etag=if_match_etag,
                ),
            ),
        )
        updated = await self.read_pending_approval(
            household_id=household_id,
            approval_id=approval_id,
        )
        if updated is None:
            raise RuntimeError("Replaced pending approval could not be read back.")
        return updated

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
