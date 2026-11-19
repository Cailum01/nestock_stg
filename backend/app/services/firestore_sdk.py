"""Firestore SDK adapters for the existing Cosmos-shaped application ports."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from google.cloud.firestore_v1 import FieldFilter, Query
from google.cloud.firestore_v1 import transactional as firestore_transactional

from app.services.cosmos_read import DocumentQueryClient
from app.services.cosmos_write import (
    BatchOperation,
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    OperationReceipt,
    ReplaceDocument,
    WriteStatus,
)
from app.services.firestore_mapping import (
    FirestoreTransactionWrite,
    OperationClaimConflict,
    TransactionWriteLimitExceeded,
    build_approved_transaction_plan,
    operation_claim_path,
    resolve_operation_claims,
)

MAX_DOCUMENT_BYTES = 1_048_576
MAX_COMMIT_BYTES = 10 * 1024 * 1024
MAX_PREFLIGHT_BYTES = 8 * 1024 * 1024
MAX_TRANSACTION_WRITES = 100
OPERATION_FINGERPRINT_VERSION = 1


def firestore_version(update_time: datetime | None) -> str | None:
    if update_time is None:
        return None
    if not isinstance(update_time, datetime):
        if hasattr(update_time, "ToDatetime"):
            update_time = update_time.ToDatetime(tzinfo=timezone.utc)
        elif hasattr(update_time, "seconds") and hasattr(update_time, "nanos"):
            update_time = datetime.fromtimestamp(
                update_time.seconds,
                tz=timezone.utc,
            ).replace(microsecond=update_time.nanos // 1000)
        else:
            raise TypeError("Firestore update_time has an unsupported SDK representation.")
    if update_time.tzinfo is None:
        update_time = update_time.replace(tzinfo=timezone.utc)
    return (
        update_time.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _snapshot_payload(snapshot: Any) -> dict[str, object] | None:
    if not snapshot.exists:
        return None
    payload = dict(snapshot.to_dict() or {})
    payload["_etag"] = firestore_version(snapshot.update_time) or ""
    return payload


def _json_size(value: object) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Firestore writes must contain canonical JSON values.") from error


def preflight_firestore_writes(
    writes: Sequence[FirestoreTransactionWrite],
    *,
    max_writes: int = MAX_TRANSACTION_WRITES,
    max_request_bytes: int = MAX_COMMIT_BYTES,
) -> None:
    """Conservatively reject commits too large for documented Firestore limits.

    The estimator doubles UTF-8 canonical JSON sizes and adds path/protobuf
    overhead, retaining margin below the documented 10 MiB API request limit.
    Current mapped records contain JSON values only and therefore use no field
    transformations. This is intentionally not a general transaction write cap.
    """
    if len(writes) > max_writes:
        raise TransactionWriteLimitExceeded(
            f"Planned write count {len(writes)} exceeds application cap {max_writes}."
        )
    estimated_request_bytes = 1024
    for write in writes:
        document_size = _json_size(write.document)
        if document_size * 2 > MAX_DOCUMENT_BYTES:
            raise ValueError(
                f"Firestore document {write.path!r} exceeds the conservative 1 MiB preflight."
            )
        # Firestore's current JSON mapper emits no server-side field transforms.
        estimated_request_bytes += (document_size * 2) + len(write.path.encode("utf-8")) + 256
    safe_limit = min(max_request_bytes, MAX_PREFLIGHT_BYTES)
    if estimated_request_bytes > safe_limit:
        raise ValueError(
            "Firestore transaction exceeds the conservative serialized-request preflight "
            f"({estimated_request_bytes} > {safe_limit} bytes)."
        )


def _firestore_document(document: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"_etag", "_attachments", "_rid", "_self", "_ts"}
    }


def _single_snapshot(transaction: Any, reference: Any) -> Any:
    result = transaction.get(reference)
    if hasattr(result, "__next__"):
        result = next(result, None)
    return result


def _run_transaction(
    client: Any,
    callback: Any,
    *,
    max_attempts: int = 5,
) -> Any:
    transaction = client.transaction(max_attempts=max_attempts)
    return firestore_transactional(callback)(transaction)


class FirestoreDocumentQueryClient(DocumentQueryClient):
    """Query the approved household records collection using existing query ports."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def read_item(
        self,
        *,
        item_id: str,
        partition_key: str,
    ) -> Mapping[str, object] | None:
        path = f"households/{partition_key}/records/{item_id}"
        snapshot = await asyncio.to_thread(self._client.document(path).get)
        return _snapshot_payload(snapshot)

    async def query_items(
        self,
        *,
        query: str,
        parameters: Sequence[Mapping[str, object]],
        partition_key: str,
    ) -> Sequence[Mapping[str, object]]:
        parameter_map = {
            str(parameter.get("name", "")).lstrip("@"): parameter.get("value")
            for parameter in parameters
        }
        query_ref = self._client.collection_group("records")
        query_ref = query_ref.where(filter=FieldFilter("household_id", "==", partition_key))
        record_type = parameter_map.get("record_type")
        if record_type is not None:
            query_ref = query_ref.where(filter=FieldFilter("record_type", "==", record_type))

        # Translate the deliberately small, parameterized SQL subset emitted by
        # CosmosReadRepository and CosmosWriteRepository. Household scope is
        # always applied independently of the SQL text.
        for field, operator, parameter_name in re.findall(
            r"\bc\.([a-z_]+)\s*(=|>=|<=|>|<)\s*@([a-z_]+)", query
        ):
            if field in {"household_id", "record_type"}:
                continue
            if parameter_name not in parameter_map:
                raise ValueError(f"Unsupported Firestore query parameter @{parameter_name}.")
            firestore_operator = "==" if operator == "=" else operator
            query_ref = query_ref.where(
                filter=FieldFilter(field, firestore_operator, parameter_map[parameter_name])
            )
        if "c.status IN" in query:
            statuses = [
                value
                for key, value in parameter_map.items()
                if key.endswith("_status") and isinstance(value, str)
            ]
            if statuses:
                query_ref = query_ref.where(filter=FieldFilter("status", "in", statuses))

        order_terms = re.search(r"ORDER BY (.+?)(?:\s+LIMIT|$)", query, flags=re.IGNORECASE)
        if order_terms:
            for field, direction in re.findall(
                r"c\.([a-z_]+)\s+(ASC|DESC)", order_terms.group(1), flags=re.IGNORECASE
            ):
                query_ref = query_ref.order_by(
                    field,
                    direction=(
                        Query.DESCENDING if direction.upper() == "DESC" else Query.ASCENDING
                    ),
                )
        limit_match = re.search(r"SELECT TOP @limit", query, flags=re.IGNORECASE)
        if limit_match and isinstance(parameter_map.get("limit"), int):
            query_ref = query_ref.limit(parameter_map["limit"])

        snapshots = await asyncio.to_thread(lambda: list(query_ref.stream()))
        return [
            payload
            for snapshot in snapshots
            if (payload := _snapshot_payload(snapshot)) is not None
        ]


class FirestoreTransactionalBatchClient:
    """Atomic one-household replacement for the existing transactional batch port."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def firestore_client(self) -> Any:
        return self._client

    async def read_item(
        self,
        *,
        item_id: str,
        partition_key: str,
    ) -> Mapping[str, object] | None:
        return await FirestoreDocumentQueryClient(self._client).read_item(
            item_id=item_id,
            partition_key=partition_key,
        )

    async def execute_batch(
        self,
        *,
        partition_key: str,
        operations: Sequence[BatchOperation],
    ) -> None:
        if not partition_key or not operations:
            raise ValueError("Firestore batch requires a household and at least one operation.")
        if len(operations) > MAX_TRANSACTION_WRITES:
            raise TransactionWriteLimitExceeded(
                f"Planned write count {len(operations)} exceeds application cap "
                f"{MAX_TRANSACTION_WRITES}."
            )
        refs: list[tuple[Any, BatchOperation]] = []
        preflight: list[FirestoreTransactionWrite] = []
        for operation in operations:
            document = dict(operation.document)
            if document.get("household_id") != partition_key:
                raise ValueError("Firestore writes must match the household partition.")
            record_id = document.get("id")
            if not isinstance(record_id, str) or not record_id:
                raise ValueError("Firestore writes require a non-empty document ID.")
            reference = self._client.document(f"households/{partition_key}/records/{record_id}")
            refs.append((reference, operation))
            kind = "replace" if isinstance(operation, ReplaceDocument) else "create"
            preflight.append(
                FirestoreTransactionWrite(
                    path=reference.path,
                    kind=kind,
                    document=_firestore_document(document),
                    expected_version=(
                        operation.if_match_etag if isinstance(operation, ReplaceDocument) else None
                    ),
                )
            )
        preflight_firestore_writes(preflight)

        def commit(transaction: Any) -> None:
            snapshots = [_single_snapshot(transaction, reference) for reference, _ in refs]
            for index, ((reference, operation), snapshot) in enumerate(
                zip(refs, snapshots, strict=True)
            ):
                exists = bool(snapshot and snapshot.exists)
                if isinstance(operation, CreateDocument) and exists:
                    raise BatchOperationConflict(index)
                if isinstance(operation, ReplaceDocument):
                    if (
                        not exists
                        or firestore_version(snapshot.update_time) != operation.if_match_etag
                    ):
                        raise BatchOperationConflict(index)
            for reference, operation in refs:
                document = _firestore_document(operation.document)
                if isinstance(operation, CreateDocument):
                    transaction.create(reference, document)
                else:
                    transaction.set(reference, document)

        try:
            await asyncio.to_thread(_run_transaction, self._client, commit)
        except BatchOperationConflict:
            raise


class FirestoreWriteRepository(CosmosWriteRepository):
    """Approval storage retaining the public Cosmos-shaped port and atomic semantics."""

    def __init__(
        self,
        client: Any,
        *,
        query_client: FirestoreDocumentQueryClient,
        transactional_client: FirestoreTransactionalBatchClient,
    ) -> None:
        super().__init__(transactional_client, query_client=query_client)
        self._firestore = client

    async def read_operation_receipt(
        self,
        *,
        household_id: str,
        operation_id: str,
    ) -> OperationReceipt | None:
        reference = self._firestore.document(operation_claim_path(operation_id))
        snapshot = await asyncio.to_thread(reference.get)
        document = _snapshot_payload(snapshot)
        if document is None:
            return None
        if document.get("household_id") != household_id:
            raise OperationIdConflict(
                f"operation_id {operation_id!r} is already claimed by another household."
            )
        if document.get("operation_id") != operation_id:
            raise OperationIdConflict("Stored global operation claim does not match its key.")
        fingerprint = document.get("request_fingerprint")
        batch_fingerprint = document.get("batch_fingerprint")
        if (
            document.get("fingerprint_version") != OPERATION_FINGERPRINT_VERSION
            or not isinstance(fingerprint, str)
            or not isinstance(batch_fingerprint, str)
        ):
            raise ValueError("Stored Firestore operation claim is malformed.")
        return OperationReceipt(
            household_id=household_id,
            operation_id=operation_id,
            request_fingerprint=fingerprint,
            batch_fingerprint=batch_fingerprint,
        )

    async def execute_idempotent_batches(
        self,
        *,
        household_id: str,
        operations_by_id: Mapping[str, Sequence[BatchOperation]],
        shared_operations: Sequence[BatchOperation] = (),
    ) -> WriteStatus:
        if not shared_operations or not isinstance(shared_operations[-1], ReplaceDocument):
            raise ValueError("Firestore approval transaction requires a final approval replace.")
        completed_approval = shared_operations[-1]
        domain_shared = shared_operations[:-1]
        outcome_by_operation = {operation_id: None for operation_id in operations_by_id}
        approval_outcome = completed_approval.document.get("outcome")
        if isinstance(approval_outcome, Mapping):
            outcome_by_operation = {
                operation_id: approval_outcome.get(operation_id)
                for operation_id in operations_by_id
            }
        plan = build_approved_transaction_plan(
            household_id=household_id,
            operations_by_id=operations_by_id,
            shared_operations=domain_shared,
            completed_approval=completed_approval,
            outcome_by_operation=outcome_by_operation,
            max_writes=MAX_TRANSACTION_WRITES,
        )
        plan = replace(
            plan,
            writes=tuple(
                replace(
                    write,
                    document={
                        **write.document,
                        "fingerprint_version": OPERATION_FINGERPRINT_VERSION,
                    },
                )
                if write.document.get("record_type") == "operation_claim"
                else write
                for write in plan.writes
            ),
        )
        preflight_firestore_writes(plan.writes)
        references = {
            path: self._firestore.document(path) for path in {read.path for read in plan.reads}
        }
        writes_by_path: dict[str, list[FirestoreTransactionWrite]] = {}
        for write in plan.writes:
            writes_by_path.setdefault(write.path, []).append(write)
        if any(len(writes) > 1 for writes in writes_by_path.values()):
            raise ValueError("Firestore atomic plan contains duplicate write paths.")
        outcome: dict[str, object] = {}
        result_status = {"value": WriteStatus.APPLIED}

        def commit(transaction: Any) -> None:
            result_status["value"] = WriteStatus.APPLIED
            outcome.clear()
            # Firestore requires all reads to precede every write. Keep this
            # callback deterministic and free of non-Firestore side effects.
            read_snapshots: dict[str, Any] = {}
            for read in plan.reads:
                read_snapshots[read.path] = _single_snapshot(transaction, references[read.path])
            claim_documents = {
                path: (
                    dict(snapshot.to_dict() or {})
                    if snapshot is not None and snapshot.exists
                    else None
                )
                for path, snapshot in read_snapshots.items()
                if path in plan.operation_claim_paths.values()
            }
            if any(
                document is not None
                and document.get("fingerprint_version") != OPERATION_FINGERPRINT_VERSION
                for document in claim_documents.values()
            ):
                raise OperationClaimConflict(
                    "Stored operation claim uses an unsupported fingerprint version."
                )
            resolution = resolve_operation_claims(plan, claim_documents)
            if resolution.status == "already_applied":
                result_status["value"] = WriteStatus.ALREADY_APPLIED
                outcome.clear()
                outcome.update(resolution.outcome_by_operation)
                return

            for read in plan.reads:
                if read.purpose not in {"replace_precondition", "approval_precondition"}:
                    continue
                snapshot = read_snapshots[read.path]
                if (
                    snapshot is None
                    or not snapshot.exists
                    or firestore_version(snapshot.update_time) != read.expected_version
                ):
                    write = next(item for item in plan.writes if item.path == read.path)
                    raise BatchOperationConflict(
                        next(
                            (
                                index
                                for index, item in enumerate(plan.writes)
                                if item.path == write.path
                            ),
                            0,
                        )
                    )
                payload = snapshot.to_dict() or {}
                if payload.get("household_id") != household_id:
                    raise ValueError("Firestore path and household field scope do not match.")

            for write in plan.writes:
                reference = references.setdefault(write.path, self._firestore.document(write.path))
                document = _firestore_document(write.document)
                if write.kind == "create":
                    transaction.create(reference, document)
                elif write.kind == "replace":
                    transaction.set(reference, document)
                else:
                    raise ValueError(f"Unknown Firestore write kind: {write.kind}.")
            outcome.clear()
            outcome.update({key: value for key, value in outcome_by_operation.items()})

        try:
            await asyncio.to_thread(_run_transaction, self._firestore, commit)
        except OperationClaimConflict as error:
            raise OperationIdConflict(str(error)) from error
        return result_status["value"]
