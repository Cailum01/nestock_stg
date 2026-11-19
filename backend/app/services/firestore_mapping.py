"""Pure Firestore document mapping and atomic-write planning.

This module deliberately has no Firestore SDK or execution code. A later adapter
must read every planned path in a transaction before applying any planned write.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar

from pydantic import JsonValue

from app.models.domain import (
    ChangeEvent,
    InventoryItem,
    MediaInputMapping,
    ShoppingItem,
    Transaction,
)
from app.models.identity import (
    DeviceRecord,
    DeviceRecoveryRecord,
    HouseholdInviteRecord,
    HouseholdMembership,
    HouseholdRecord,
    HouseholdRecoveryRecord,
)
from app.services.approvals import ApprovalRecord
from app.services.cosmos_read import DomainRecord
from app.services.cosmos_write import BatchOperation, CreateDocument, ReplaceDocument

DEFAULT_MAX_TRANSACTION_WRITES = 100
"""Application safety cap carried forward from Cosmos; not a Firestore quota."""

_DOMAIN_IDENTITIES: dict[type[DomainRecord], tuple[str, str]] = {
    InventoryItem: ("inventory_item", "item_id"),
    ShoppingItem: ("shopping_item", "shopping_item_id"),
    Transaction: ("transaction", "transaction_id"),
    ChangeEvent: ("change_event", "event_id"),
    MediaInputMapping: ("user_input", "input_id"),
}
_DOMAIN_ID_NAMESPACES = {MediaInputMapping: "input"}
_COSMOS_FIELDS = frozenset({"_attachments", "_etag", "_rid", "_self", "_ts", "ttl"})
_PRICE_FIELDS = frozenset({"price", "estimated_price", "unit_price", "currency"})
RecordT = TypeVar("RecordT")


class FirestoreMappingError(ValueError):
    """A record cannot be represented by the approved Firestore mapping."""


class OperationClaimConflict(ValueError):
    """An operation ID is claimed by another household or different payload."""


class OperationClaimInconsistency(ValueError):
    """Only part of an expected operation-claim set exists."""


class TransactionWriteLimitExceeded(ValueError):
    """The planned writes exceed the configured application hard cap."""


@dataclass(frozen=True)
class MappedFirestoreDocument:
    path: str
    document: Mapping[str, object]


@dataclass(frozen=True)
class FirestoreTransactionRead:
    path: str
    purpose: str
    expected_version: str | None = None


@dataclass(frozen=True)
class FirestoreTransactionWrite:
    path: str
    kind: str
    document: Mapping[str, object]
    operation_id: str | None = None
    expected_version: str | None = None


@dataclass(frozen=True)
class FirestoreTransactionPlan:
    """Complete read set and write set; the read set always precedes the writes."""

    household_id: str
    reads: tuple[FirestoreTransactionRead, ...]
    writes: tuple[FirestoreTransactionWrite, ...]
    operation_claim_paths: Mapping[str, str]
    request_fingerprints: Mapping[str, str]
    batch_fingerprint: str


@dataclass(frozen=True)
class FirestoreTransactionResolution:
    """A transaction callback's pure decision after reading all planned paths."""

    status: str
    writes: tuple[FirestoreTransactionWrite, ...]
    outcome_by_operation: Mapping[str, JsonValue]


def _segment(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or value in {".", ".."}:
        raise FirestoreMappingError(f"{name} must be a valid Firestore path segment.")
    if len(value) >= 4 and value.startswith("__") and value.endswith("__"):
        raise FirestoreMappingError(f"{name} must not match Firestore's reserved __.*__ pattern.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FirestoreMappingError(f"{name} must be valid UTF-8.") from error
    if len(encoded) > 1500:
        raise FirestoreMappingError(f"{name} must not exceed 1500 UTF-8 bytes.")
    return value


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FirestoreMappingError("Persisted timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_timestamps(document: dict[str, object], *fields: str) -> None:
    for field in fields:
        value = document.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise FirestoreMappingError(f"{field} must be a serialized timestamp.")
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise FirestoreMappingError(f"{field} must be a valid timestamp.") from error
            document[field] = _utc_isoformat(timestamp)


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise FirestoreMappingError("Persisted values must be canonical JSON.") from error


def _assert_no_price_fields(value: object, path: str = "document") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in _PRICE_FIELDS:
                raise FirestoreMappingError(
                    f"Price-related field is not persistable: {path}.{key}."
                )
            _assert_no_price_fields(child, f"{path}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            _assert_no_price_fields(child, f"{path}[{index}]")


def _identity_document(
    *,
    path: str,
    document: dict[str, object],
) -> MappedFirestoreDocument:
    _assert_no_price_fields(document)
    return MappedFirestoreDocument(path=path, document=document)


def _membership_document_id(installation_id: str, household_id: str) -> str:
    identity = _stable_json([installation_id, household_id]).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def operation_claim_path(operation_id: str) -> str:
    _segment(operation_id, "operation_id")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"operation_claims/{digest}"


class FirestoreDocumentMapper:
    """Map existing domain and identity contracts to the approved path layout."""

    @staticmethod
    def to_document(record: object) -> MappedFirestoreDocument:
        if type(record) in _DOMAIN_IDENTITIES:
            return FirestoreDocumentMapper._domain_to_document(record)  # type: ignore[arg-type]
        if isinstance(record, ApprovalRecord):
            household_id = _segment(record.household_id, "household_id")
            approval_id = _segment(record.approval_id, "approval_id")
            record_id = _segment(f"pending_approval:{approval_id}", "record_id")
            document = record.model_dump(mode="json")
            _normalize_timestamps(
                document,
                "created_at",
                "expires_at",
                "decided_at",
                "completed_at",
            )
            document.update(
                id=record_id,
                record_type="pending_approval",
                household_id=household_id,
            )
            return _identity_document(
                path=f"households/{household_id}/records/{record_id}",
                document=document,
            )
        if isinstance(record, HouseholdRecord):
            household_id = _segment(record.household_id, "household_id")
            document = record.model_dump(mode="json")
            _normalize_timestamps(document, "created_at")
            document.update(
                id=_segment(household_id, "household_id"),
                record_type="household",
            )
            return _identity_document(
                path=f"households/{household_id}",
                document=document,
            )
        if isinstance(record, DeviceRecord):
            installation_id = _segment(record.device_id, "device_id")
            document = record.model_dump(mode="json")
            _normalize_timestamps(document, "created_at", "revoked_at")
            document["installation_id"] = document.pop("device_id")
            document.update(id=_segment(installation_id, "device_id"), record_type="installation")
            return _identity_document(
                path=f"installations/{installation_id}",
                document=document,
            )
        if isinstance(record, HouseholdMembership):
            installation_id = _segment(record.device_id, "device_id")
            household_id = _segment(record.household_id, "household_id")
            document = record.model_dump(mode="json")
            _normalize_timestamps(document, "created_at", "revoked_at")
            document["installation_id"] = document.pop("device_id")
            document.update(
                id=_membership_document_id(installation_id, household_id),
                record_type="household_membership",
            )
            return _identity_document(
                path=f"household_memberships/{document['id']}",
                document=document,
            )
        if isinstance(record, HouseholdInviteRecord):
            household_id = _segment(record.household_id, "household_id")
            invite_id = _segment(record.invite_id, "invite_id")
            document = record.model_dump(mode="json")
            _normalize_timestamps(document, "created_at", "expires_at", "redeemed_at")
            document.update(id=_segment(invite_id, "invite_id"), record_type="household_invite")
            return _identity_document(
                path=f"households/{household_id}/invites/{invite_id}",
                document=document,
            )
        if isinstance(record, HouseholdRecoveryRecord):
            household_id = _segment(record.household_id, "household_id")
            recovery_id = _segment(record.recovery_id, "recovery_id")
            document = record.model_dump(mode="json")
            _normalize_timestamps(document, "created_at", "consumed_at")
            document.update(
                id=_segment(recovery_id, "recovery_id"),
                record_type="household_recovery",
            )
            return _identity_document(
                path=f"households/{household_id}/recoveries/{recovery_id}",
                document=document,
            )
        if isinstance(record, DeviceRecoveryRecord):
            installation_id = _segment(record.device_id, "device_id")
            recovery_id = _segment(record.recovery_id, "recovery_id")
            document = record.model_dump(mode="json")
            _normalize_timestamps(document, "created_at", "consumed_at")
            document["installation_id"] = document.pop("device_id")
            document.update(
                id=_segment(recovery_id, "recovery_id"),
                record_type="installation_recovery",
            )
            return _identity_document(
                path=f"installations/{installation_id}/recoveries/{recovery_id}",
                document=document,
            )
        raise FirestoreMappingError(f"Unsupported persisted record type: {type(record).__name__}.")

    @staticmethod
    def from_document(
        model_type: type[RecordT],
        *,
        path: str,
        document: Mapping[str, object],
        household_id: str | None = None,
    ) -> RecordT:
        """Validate a mapped document's identity and household scope before parsing it."""
        requested_household_id = household_id
        if model_type in _DOMAIN_IDENTITIES:
            expected_type, identity_field = _DOMAIN_IDENTITIES[model_type]  # type: ignore[index]
            actual_household = _segment(document.get("household_id"), "household_id")
            if household_id is not None and actual_household != _segment(
                household_id, "household_id"
            ):
                raise FirestoreMappingError("Document household_id does not match the read scope.")
            public_id = _segment(document.get(identity_field), identity_field)
            record_id = _segment(
                f"{_DOMAIN_ID_NAMESPACES.get(model_type, expected_type)}:{public_id}",
                "record_id",
            )
            expected_path = (
                f"households/{_segment(actual_household, 'household_id')}/records/"
                f"{_segment(record_id, 'record_id')}"
            )
            if (
                document.get("id") != record_id
                or document.get("record_type") != expected_type
                or path != expected_path
            ):
                raise FirestoreMappingError(
                    "Document path or record_type does not match its identity."
                )
            data = {
                key: value
                for key, value in document.items()
                if key not in {"id", "record_type"} and key not in _COSMOS_FIELDS
            }
            _assert_no_price_fields(data)
            return model_type.model_validate(data)  # type: ignore[attr-defined, no-any-return]

        if model_type is HouseholdMembership:
            actual_household = _segment(document.get("household_id"), "household_id")
            installation_id = _segment(document.get("installation_id"), "installation_id")
            if household_id is not None and actual_household != _segment(
                household_id, "household_id"
            ):
                raise FirestoreMappingError("Membership household scope is invalid.")
            expected_id = _membership_document_id(installation_id, actual_household)
            if (
                path != f"household_memberships/{expected_id}"
                or document.get("id") != expected_id
                or document.get("record_type") != "household_membership"
            ):
                raise FirestoreMappingError(
                    "Membership path or record_type does not match its key."
                )
            data = {
                key: value for key, value in document.items() if key not in {"id", "record_type"}
            }
            data["device_id"] = data.pop("installation_id")
            _assert_no_price_fields(data)
            return model_type.model_validate(data)  # type: ignore[attr-defined, no-any-return]

        if model_type is HouseholdRecord:
            actual_household_id = _segment(document.get("household_id"), "household_id")
            expected_path = f"households/{actual_household_id}"
            expected_id = actual_household_id
            expected_type = "household"
        elif model_type is DeviceRecord:
            actual_household_id = None
            installation_id = _segment(document.get("installation_id"), "installation_id")
            expected_path = f"installations/{installation_id}"
            expected_id = installation_id
            expected_type = "installation"
        elif model_type is HouseholdInviteRecord:
            actual_household_id = _segment(document.get("household_id"), "household_id")
            expected_id = _segment(document.get("invite_id"), "invite_id")
            expected_path = f"households/{actual_household_id}/invites/{expected_id}"
            expected_type = "household_invite"
        elif model_type is HouseholdRecoveryRecord:
            actual_household_id = _segment(document.get("household_id"), "household_id")
            expected_id = _segment(document.get("recovery_id"), "recovery_id")
            expected_path = f"households/{actual_household_id}/recoveries/{expected_id}"
            expected_type = "household_recovery"
        elif model_type is DeviceRecoveryRecord:
            actual_household_id = None
            installation_id = _segment(document.get("installation_id"), "installation_id")
            expected_id = _segment(document.get("recovery_id"), "recovery_id")
            expected_path = f"installations/{installation_id}/recoveries/{expected_id}"
            expected_type = "installation_recovery"
        elif model_type is ApprovalRecord:
            actual_household_id = _segment(document.get("household_id"), "household_id")
            approval_id = _segment(document.get("approval_id"), "approval_id")
            expected_id = _segment(f"pending_approval:{approval_id}", "record_id")
            expected_path = f"households/{actual_household_id}/records/{expected_id}"
            expected_type = "pending_approval"
        else:
            raise FirestoreMappingError(f"Unsupported Firestore read model: {model_type.__name__}.")

        if requested_household_id is not None:
            requested_household_id = _segment(requested_household_id, "household_id")
            if actual_household_id != requested_household_id:
                raise FirestoreMappingError("Document household_id does not match the read scope.")
        if (
            document.get("id") != expected_id
            or path != expected_path
            or document.get("record_type") != expected_type
        ):
            raise FirestoreMappingError("Document path or record_type does not match its identity.")
        data = {
            key: value
            for key, value in document.items()
            if key not in {"id", "record_type"} and key not in _COSMOS_FIELDS
        }
        if model_type in {DeviceRecoveryRecord, HouseholdRecoveryRecord}:
            for field in ("failed_at", "cooldown_until", "expires_at"):
                data.pop(field, None)
        if model_type is DeviceRecord:
            data["device_id"] = data.pop("installation_id")
        elif model_type is DeviceRecoveryRecord:
            data["device_id"] = data.pop("installation_id")
        _assert_no_price_fields(data)
        return model_type.model_validate(data)  # type: ignore[attr-defined, no-any-return]

    @staticmethod
    def _domain_to_document(record: DomainRecord) -> MappedFirestoreDocument:
        record_type, identity_field = _DOMAIN_IDENTITIES[type(record)]
        household_id = _segment(record.household_id, "household_id")
        public_id = _segment(getattr(record, identity_field), identity_field)
        record_id = _segment(
            f"{_DOMAIN_ID_NAMESPACES.get(type(record), record_type)}:{public_id}",
            "record_id",
        )
        document = record.model_dump(mode="json")
        _normalize_timestamps(
            document,
            "created_at",
            "updated_at",
            "occurred_at",
            "recorded_at",
            "upload_authorization_expires_at",
        )
        document.update(id=record_id, record_type=record_type, household_id=household_id)
        return _identity_document(
            path=f"households/{household_id}/records/{record_id}",
            document=document,
        )


def _canonical_operation_fingerprint(operations: Sequence[BatchOperation]) -> str:
    canonical_operations = [
        {
            "kind": "create" if isinstance(operation, CreateDocument) else "replace",
            "document": dict(operation.document),
            "expected_version": (
                operation.if_match_etag if isinstance(operation, ReplaceDocument) else None
            ),
        }
        for operation in operations
    ]
    return hashlib.sha256(_stable_json(canonical_operations).encode("utf-8")).hexdigest()


def _batch_fingerprint(
    operation_fingerprints: Mapping[str, str],
    shared_operations_fingerprint: str | None,
) -> str:
    payload = {
        "operations": [
            {"operation_id": operation_id, "request_fingerprint": fingerprint}
            for operation_id, fingerprint in sorted(operation_fingerprints.items())
        ],
        "shared_operations_fingerprint": shared_operations_fingerprint,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _validated_write_document(
    operation: BatchOperation,
    *,
    household_id: str,
) -> tuple[str, dict[str, object], str, str | None]:
    document = dict(operation.document)
    if document.get("household_id") != household_id:
        raise FirestoreMappingError("Every transaction write must match the household scope.")
    record_id = document.get("id")
    record_type = document.get("record_type")
    if not isinstance(record_id, str) or not record_id:
        raise FirestoreMappingError("Every transaction write needs a non-empty id.")
    if not isinstance(record_type, str) or not record_type:
        raise FirestoreMappingError("Every transaction write needs a record_type.")
    record_id = _segment(record_id, "record_id")
    _assert_no_price_fields(document)
    path = f"households/{household_id}/records/{record_id}"
    expected_version = None
    kind = "create"
    if isinstance(operation, ReplaceDocument):
        if not operation.if_match_etag:
            raise FirestoreMappingError("Replacement writes require an expected version token.")
        expected_version = operation.if_match_etag
        kind = "replace"
    clean_document = {key: value for key, value in document.items() if key not in _COSMOS_FIELDS}
    return path, clean_document, kind, expected_version


def build_approved_transaction_plan(
    *,
    household_id: str,
    operations_by_id: Mapping[str, Sequence[BatchOperation]],
    shared_operations: Sequence[BatchOperation],
    completed_approval: ReplaceDocument,
    outcome_by_operation: Mapping[str, JsonValue],
    max_writes: int = DEFAULT_MAX_TRANSACTION_WRITES,
) -> FirestoreTransactionPlan:
    """Plan claims, domain writes and approval completion for one atomic commit.

    The cap is an application policy, not a claim about a Firestore platform limit.
    """
    household_id = _segment(household_id, "household_id")
    if not operations_by_id:
        raise FirestoreMappingError("A transaction must contain at least one operation.")
    if isinstance(max_writes, bool) or not isinstance(max_writes, int) or max_writes <= 0:
        raise FirestoreMappingError("max_writes must be a positive integer.")
    if set(operations_by_id) != set(outcome_by_operation):
        raise FirestoreMappingError("Every operation must have exactly one stored outcome.")

    fingerprints: dict[str, str] = {}
    operation_claim_paths: dict[str, str] = {}
    claim_writes: list[FirestoreTransactionWrite] = []
    reads: list[FirestoreTransactionRead] = []
    for operation_id, operations in sorted(operations_by_id.items()):
        operation_id = _segment(operation_id, "operation_id")
        if not operations:
            raise FirestoreMappingError("Every operation ID must contain at least one write.")
        for operation in operations:
            _validated_write_document(operation, household_id=household_id)
        fingerprint = _canonical_operation_fingerprint(operations)
        fingerprints[operation_id] = fingerprint
        claim_path = operation_claim_path(operation_id)
        operation_claim_paths[operation_id] = claim_path
        reads.append(FirestoreTransactionRead(path=claim_path, purpose="operation_claim"))

    for operation in shared_operations:
        _validated_write_document(operation, household_id=household_id)

    shared_fingerprint = (
        _canonical_operation_fingerprint(shared_operations) if shared_operations else None
    )
    batch_fingerprint = _batch_fingerprint(fingerprints, shared_fingerprint)

    for operation_id in sorted(operations_by_id):
        claim_path = operation_claim_paths[operation_id]
        claim_id = claim_path.rsplit("/", 1)[1]
        claim_document = {
            "id": claim_id,
            "record_type": "operation_claim",
            "household_id": household_id,
            "operation_id": operation_id,
            "request_fingerprint": fingerprints[operation_id],
            "batch_fingerprint": batch_fingerprint,
            "outcome": outcome_by_operation[operation_id],
        }
        _assert_no_price_fields(claim_document)
        claim_writes.append(
            FirestoreTransactionWrite(
                path=claim_path,
                kind="create",
                document=claim_document,
                operation_id=operation_id,
            )
        )

    domain_writes: list[FirestoreTransactionWrite] = []
    replace_reads: dict[str, FirestoreTransactionRead] = {}
    for operation_id, operations in sorted(operations_by_id.items()):
        for operation in operations:
            path, document, kind, expected_version = _validated_write_document(
                operation,
                household_id=household_id,
            )
            if kind == "replace":
                read = FirestoreTransactionRead(
                    path=path,
                    purpose="replace_precondition",
                    expected_version=expected_version,
                )
                previous = replace_reads.get(path)
                if previous is not None and previous.expected_version != expected_version:
                    raise FirestoreMappingError(
                        f"Conflicting expected versions for replacement path {path!r}."
                    )
                replace_reads[path] = read
            domain_writes.append(
                FirestoreTransactionWrite(
                    path=path,
                    kind=kind,
                    document=document,
                    operation_id=operation_id,
                    expected_version=expected_version,
                )
            )

    shared_writes: list[FirestoreTransactionWrite] = []
    for operation in shared_operations:
        path, document, kind, expected_version = _validated_write_document(
            operation,
            household_id=household_id,
        )
        if kind == "replace":
            read = FirestoreTransactionRead(
                path=path,
                purpose="replace_precondition",
                expected_version=expected_version,
            )
            previous = replace_reads.get(path)
            if previous is not None and previous.expected_version != expected_version:
                raise FirestoreMappingError(
                    f"Conflicting expected versions for replacement path {path!r}."
                )
            replace_reads[path] = read
        shared_writes.append(
            FirestoreTransactionWrite(
                path=path,
                kind=kind,
                document=document,
                expected_version=expected_version,
            )
        )

    approval_path, approval_document, approval_kind, approval_version = _validated_write_document(
        completed_approval,
        household_id=household_id,
    )
    if (
        approval_kind != "replace"
        or approval_document.get("record_type") != "pending_approval"
        or approval_document.get("status") != "completed"
    ):
        raise FirestoreMappingError("The final approval write must replace a completed card.")
    approval_read = FirestoreTransactionRead(
        path=approval_path,
        purpose="approval_precondition",
        expected_version=approval_version,
    )
    if approval_path in replace_reads and replace_reads[approval_path] != approval_read:
        raise FirestoreMappingError("Approval path has conflicting replacement preconditions.")
    replace_reads[approval_path] = approval_read

    writes = (
        *claim_writes,
        *domain_writes,
        *shared_writes,
        FirestoreTransactionWrite(
            path=approval_path,
            kind="replace",
            document=approval_document,
            expected_version=approval_version,
        ),
    )
    if len(writes) > max_writes:
        raise TransactionWriteLimitExceeded(
            f"Planned write count {len(writes)} exceeds application cap {max_writes}."
        )

    reads.extend(
        read for path, read in replace_reads.items() if path not in operation_claim_paths.values()
    )
    return FirestoreTransactionPlan(
        household_id=household_id,
        reads=tuple(reads),
        writes=tuple(writes),
        operation_claim_paths=operation_claim_paths,
        request_fingerprints=fingerprints,
        batch_fingerprint=batch_fingerprint,
    )


def resolve_operation_claims(
    plan: FirestoreTransactionPlan,
    claim_documents: Mapping[str, Mapping[str, object] | None],
) -> FirestoreTransactionResolution:
    """Resolve claim reads without side effects before a transaction callback writes."""
    expected_paths = set(plan.operation_claim_paths.values())
    if set(claim_documents) != expected_paths:
        raise OperationClaimInconsistency(
            "Claim reads must include exactly the planned claim paths."
        )

    documents = [claim_documents[path] for path in expected_paths]
    present = [document is not None for document in documents]
    if not any(present):
        return FirestoreTransactionResolution(
            status="commit",
            writes=plan.writes,
            outcome_by_operation={},
        )
    if not all(present):
        raise OperationClaimInconsistency("Only part of the operation claim set exists.")

    stored_outcomes: dict[str, JsonValue] = {}
    for operation_id, path in plan.operation_claim_paths.items():
        document = claim_documents[path]
        assert document is not None
        expected_id = path.rsplit("/", 1)[1]
        if (
            document.get("id") != expected_id
            or document.get("record_type") != "operation_claim"
            or document.get("operation_id") != operation_id
        ):
            raise OperationClaimConflict("Stored operation claim does not match its identity.")
        if document.get("household_id") != plan.household_id:
            raise OperationClaimConflict(
                f"operation_id {operation_id!r} is already claimed by another household."
            )
        if (
            document.get("request_fingerprint") != plan.request_fingerprints[operation_id]
            or document.get("batch_fingerprint") != plan.batch_fingerprint
        ):
            raise OperationClaimConflict(
                f"operation_id {operation_id!r} was reused with different fingerprints."
            )
        outcome = document.get("outcome")
        try:
            _stable_json(outcome)
        except FirestoreMappingError as error:
            raise OperationClaimInconsistency(
                f"operation_id {operation_id!r} has no valid stored outcome."
            ) from error
        _assert_no_price_fields(outcome)
        stored_outcomes[operation_id] = outcome  # type: ignore[assignment]

    return FirestoreTransactionResolution(
        status="already_applied",
        writes=(),
        outcome_by_operation=stored_outcomes,
    )
