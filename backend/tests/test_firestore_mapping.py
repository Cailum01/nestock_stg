"""Unit tests for Firestore path mapping and transaction planning only.

These tests do not execute Firestore reads, writes, transactions, or retries.
"""

import hashlib
from datetime import UTC, datetime

import pytest

from app.models.agent import ApprovalPresentation
from app.models.domain import InventoryItem
from app.models.identity import (
    DeviceRecord,
    DeviceRecoveryRecord,
    HouseholdInviteRecord,
    HouseholdMembership,
    HouseholdRecord,
    HouseholdRecoveryRecord,
    HouseholdRole,
)
from app.services.approvals import ApprovalRecord, ApprovalStatus, StoredOperation
from app.services.cosmos_write import CreateDocument, ReplaceDocument
from app.services.firestore_mapping import (
    DEFAULT_MAX_TRANSACTION_WRITES,
    FirestoreDocumentMapper,
    FirestoreMappingError,
    OperationClaimConflict,
    OperationClaimInconsistency,
    TransactionWriteLimitExceeded,
    build_approved_transaction_plan,
    operation_claim_path,
    resolve_operation_claims,
)

NOW = datetime(2026, 11, 19, 5, 0, tzinfo=UTC)
HOME = "home-1"


def inventory(*, household_id=HOME, quantity=2):
    return {
        "id": "inventory_item:rice",
        "record_type": "inventory_item",
        "household_id": household_id,
        "item_id": "rice",
        "name": "Rice",
        "quantity": quantity,
        "unit": "bag",
        "created_at": "2026-11-19T05:00:00+00:00",
        "updated_at": "2026-11-19T05:00:00+00:00",
    }


def make_plan(
    *,
    operations_by_id=None,
    max_writes=DEFAULT_MAX_TRANSACTION_WRITES,
    domain_version="domain-version-1",
    shared_operations=(),
):
    operations_by_id = operations_by_id or {
        "op-1": (
            CreateDocument(
                {
                    "id": "transaction:tx-1",
                    "record_type": "transaction",
                    "household_id": HOME,
                    "transaction_id": "tx-1",
                    "operation_id": "op-1",
                    "quantity_delta": 1,
                }
            ),
            ReplaceDocument(
                inventory(quantity=3),
                if_match_etag=domain_version,
            ),
        )
    }
    outcomes = {
        operation_id: {"transaction_id": f"tx-{operation_id}"} for operation_id in operations_by_id
    }
    return build_approved_transaction_plan(
        household_id=HOME,
        operations_by_id=operations_by_id,
        shared_operations=shared_operations,
        completed_approval=ReplaceDocument(
            {
                "id": "pending_approval:approval-1",
                "record_type": "pending_approval",
                "household_id": HOME,
                "approval_id": "approval-1",
                "status": "completed",
                "outcome": outcomes,
            },
            if_match_etag="approval-version-1",
        ),
        outcome_by_operation=outcomes,
        max_writes=max_writes,
    )


def test_domain_record_path_mapping_round_trips_and_checks_household_scope():
    record = InventoryItem(
        item_id="rice",
        household_id=HOME,
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )

    mapped = FirestoreDocumentMapper.to_document(record)

    assert mapped.path == "households/home-1/records/inventory_item:rice"
    assert mapped.document["id"] == "inventory_item:rice"
    assert mapped.document["record_type"] == "inventory_item"
    assert mapped.document["household_id"] == HOME
    assert mapped.document["created_at"] == "2026-11-19T05:00:00.000000Z"
    assert (
        FirestoreDocumentMapper.from_document(
            InventoryItem,
            path=mapped.path,
            document=mapped.document,
            household_id=HOME,
        )
        == record
    )

    with pytest.raises(FirestoreMappingError, match="household_id"):
        FirestoreDocumentMapper.from_document(
            InventoryItem,
            path=mapped.path,
            document=mapped.document | {"household_id": "other"},
            household_id=HOME,
        )


def test_domain_mapper_rejects_path_type_mismatch_and_price_fields():
    mapped = FirestoreDocumentMapper.to_document(
        InventoryItem(
            item_id="rice",
            household_id=HOME,
            name="Rice",
            quantity=2,
            unit="bag",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    with pytest.raises(FirestoreMappingError, match="path or record_type"):
        FirestoreDocumentMapper.from_document(
            InventoryItem,
            path="households/other/records/inventory_item:rice",
            document=mapped.document,
        )

    with pytest.raises(FirestoreMappingError, match="Price-related"):
        FirestoreDocumentMapper.from_document(
            InventoryItem,
            path=mapped.path,
            document=mapped.document | {"estimated_price": 42.0},
        )


@pytest.mark.parametrize(
    ("household_id", "error"),
    [
        ("__reserved__", "reserved"),
        ("家" * 501, "1500 UTF-8 bytes"),
    ],
)
def test_mapper_rejects_reserved_and_overlong_utf8_path_ids(household_id, error):
    with pytest.raises(FirestoreMappingError, match=error):
        FirestoreDocumentMapper.to_document(
            HouseholdRecord(
                household_id=household_id,
                created_by_device_id="install-1",
                created_at=NOW,
            )
        )

    document = {
        "id": household_id,
        "record_type": "household",
        "household_id": household_id,
        "created_by_device_id": "install-1",
        "created_at": NOW.isoformat(),
    }
    with pytest.raises(FirestoreMappingError, match=error):
        FirestoreDocumentMapper.from_document(
            HouseholdRecord,
            path=f"households/{household_id}",
            document=document,
        )


def test_mapper_accepts_path_id_at_the_1500_utf8_byte_boundary():
    household_id = "家" * 500
    mapped = FirestoreDocumentMapper.to_document(
        HouseholdRecord(
            household_id=household_id,
            created_by_device_id="install-1",
            created_at=NOW,
        )
    )

    assert len(household_id.encode("utf-8")) == 1500
    assert mapped.path == f"households/{household_id}"


def test_approval_record_maps_to_the_household_records_collection():
    approval = ApprovalRecord(
        approval_id="approval-1",
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[
            StoredOperation(
                operation_id="op-1",
                action="complete_purchase",
                description="Confirm purchase",
                values={"quantity": 1},
            )
        ],
        status=ApprovalStatus.CONFIRMED,
        created_at=NOW,
        expires_at=NOW.replace(hour=6),
        decided_at=NOW,
    )

    mapped = FirestoreDocumentMapper.to_document(approval)

    assert mapped.path == "households/home-1/records/pending_approval:approval-1"
    assert mapped.document["record_type"] == "pending_approval"
    assert mapped.document["household_id"] == HOME
    assert (
        FirestoreDocumentMapper.from_document(
            ApprovalRecord,
            path=mapped.path,
            document=mapped.document,
            household_id=HOME,
        )
        == approval
    )


def test_membership_path_is_deterministic_per_installation_and_household_pair():
    membership = HouseholdMembership(
        household_id=HOME,
        device_id="install-1",
        role=HouseholdRole.MEMBER,
        created_at=NOW,
    )

    mapped = FirestoreDocumentMapper.to_document(membership)
    repeat = FirestoreDocumentMapper.to_document(membership)
    another_household = FirestoreDocumentMapper.to_document(
        membership.model_copy(update={"household_id": "home-2"})
    )
    another_installation = FirestoreDocumentMapper.to_document(
        membership.model_copy(update={"device_id": "install-2"})
    )

    assert mapped.path.startswith("household_memberships/")
    assert mapped == repeat
    assert mapped.path != another_household.path
    assert mapped.path != another_installation.path
    assert mapped.document["installation_id"] == "install-1"
    assert mapped.document["household_id"] == HOME
    assert mapped.document["record_type"] == "household_membership"
    assert "device_id" not in mapped.document
    assert (
        FirestoreDocumentMapper.from_document(
            HouseholdMembership,
            path=mapped.path,
            document=mapped.document,
            household_id=HOME,
        )
        == membership
    )


def test_identity_records_map_to_their_scoped_paths():
    household = FirestoreDocumentMapper.to_document(
        HouseholdRecord(
            household_id=HOME,
            created_by_device_id="install-1",
            created_at=NOW,
        )
    )
    device = FirestoreDocumentMapper.to_document(
        DeviceRecord(
            device_id="install-1",
            credential_hash="a" * 64,
            created_at=NOW,
        )
    )
    invite = FirestoreDocumentMapper.to_document(
        HouseholdInviteRecord(
            invite_id="invite-1",
            household_id=HOME,
            token_hash="b" * 64,
            created_by_device_id="install-1",
            created_at=NOW,
            expires_at=NOW.replace(minute=10),
        )
    )
    household_recovery = FirestoreDocumentMapper.to_document(
        HouseholdRecoveryRecord(
            recovery_id="recovery-1",
            household_id=HOME,
            code_hash="c" * 64,
            created_at=NOW,
        )
    )
    device_recovery = FirestoreDocumentMapper.to_document(
        DeviceRecoveryRecord(
            recovery_id="recovery-2",
            device_id="install-1",
            code_hash="d" * 64,
            created_at=NOW,
        )
    )

    assert household.path == "households/home-1"
    assert household.document["record_type"] == "household"
    assert device.path == "installations/install-1"
    assert device.document["installation_id"] == "install-1"
    assert invite.path == "households/home-1/invites/invite-1"
    assert invite.document["household_id"] == HOME
    assert household_recovery.path == "households/home-1/recoveries/recovery-1"
    assert household_recovery.document["household_id"] == HOME
    assert device_recovery.path == "installations/install-1/recoveries/recovery-2"


def test_claim_paths_use_global_sha256_operation_identity():
    assert operation_claim_path("op-1") == (
        "operation_claims/" + hashlib.sha256(b"op-1").hexdigest()
    )


def test_approved_plan_contains_claims_domain_and_final_approval_writes():
    plan = make_plan()

    assert plan.household_id == HOME
    assert [read.purpose for read in plan.reads] == [
        "operation_claim",
        "replace_precondition",
        "approval_precondition",
    ]
    assert plan.reads[0].path == operation_claim_path("op-1")
    assert plan.reads[1].path == "households/home-1/records/inventory_item:rice"
    assert plan.reads[1].expected_version == "domain-version-1"
    assert plan.reads[2].path == "households/home-1/records/pending_approval:approval-1"
    assert [write.kind for write in plan.writes] == ["create", "create", "replace", "replace"]
    assert plan.writes[0].document["record_type"] == "operation_claim"
    assert plan.writes[0].document["household_id"] == HOME
    assert plan.writes[-1].document["status"] == "completed"
    assert plan.writes[-1].path.endswith("pending_approval:approval-1")


def test_claim_resolution_retries_with_stored_outcome_and_no_writes():
    plan = make_plan()
    claim = dict(plan.writes[0].document) | {"outcome": {"stored": "outcome"}}

    result = resolve_operation_claims(
        plan,
        {plan.operation_claim_paths["op-1"]: claim},
    )

    assert result.status == "already_applied"
    assert result.writes == ()
    assert result.outcome_by_operation == {"op-1": {"stored": "outcome"}}


def test_expected_operation_version_changes_fingerprint_and_rejects_old_claim():
    original_plan = make_plan(domain_version="domain-version-1")
    changed_plan = make_plan(domain_version="domain-version-2")
    original_claim = dict(original_plan.writes[0].document)

    assert original_plan.request_fingerprints["op-1"] != changed_plan.request_fingerprints["op-1"]
    with pytest.raises(OperationClaimConflict, match="different fingerprints"):
        resolve_operation_claims(
            changed_plan,
            {changed_plan.operation_claim_paths["op-1"]: original_claim},
        )

    unchanged_retry = resolve_operation_claims(
        original_plan,
        {original_plan.operation_claim_paths["op-1"]: original_claim},
    )
    assert unchanged_retry.status == "already_applied"
    assert unchanged_retry.writes == ()


def test_expected_shared_write_version_changes_batch_fingerprint():
    shared_document = {
        "id": "shopping_item:milk",
        "record_type": "shopping_item",
        "household_id": HOME,
        "shopping_item_id": "milk",
        "status": "pending",
    }
    original_plan = make_plan(
        shared_operations=(ReplaceDocument(shared_document, if_match_etag="shared-version-1"),)
    )
    changed_plan = make_plan(
        shared_operations=(ReplaceDocument(shared_document, if_match_etag="shared-version-2"),)
    )

    assert original_plan.request_fingerprints == changed_plan.request_fingerprints
    assert original_plan.batch_fingerprint != changed_plan.batch_fingerprint
    with pytest.raises(OperationClaimConflict, match="different fingerprints"):
        resolve_operation_claims(
            changed_plan,
            {changed_plan.operation_claim_paths["op-1"]: dict(original_plan.writes[0].document)},
        )


def test_claim_resolution_commits_when_claims_are_absent():
    plan = make_plan()

    result = resolve_operation_claims(
        plan,
        {plan.operation_claim_paths["op-1"]: None},
    )

    assert result.status == "commit"
    assert result.writes == plan.writes
    assert result.outcome_by_operation == {}


def test_claim_resolution_rejects_cross_household_replay_and_fingerprint_mismatch():
    plan = make_plan()
    path = plan.operation_claim_paths["op-1"]
    claim = dict(plan.writes[0].document)

    with pytest.raises(OperationClaimConflict, match="another household"):
        resolve_operation_claims(plan, {path: claim | {"household_id": "home-2"}})

    with pytest.raises(OperationClaimConflict, match="different fingerprints"):
        resolve_operation_claims(plan, {path: claim | {"request_fingerprint": "0" * 64}})


def test_claim_resolution_rejects_partial_claim_sets():
    operations_by_id = {
        operation_id: (
            CreateDocument(
                {
                    "id": f"transaction:tx-{operation_id}",
                    "record_type": "transaction",
                    "household_id": HOME,
                    "operation_id": operation_id,
                }
            ),
        )
        for operation_id in ("op-1", "op-2")
    }
    plan = make_plan(operations_by_id=operations_by_id)
    first_operation = sorted(plan.operation_claim_paths)[0]
    first_path = plan.operation_claim_paths[first_operation]
    first_claim = next(write.document for write in plan.writes if write.path == first_path)

    with pytest.raises(OperationClaimInconsistency, match="Only part"):
        resolve_operation_claims(
            plan,
            {
                plan.operation_claim_paths["op-1"]: first_claim,
                plan.operation_claim_paths["op-2"]: None,
            },
        )


def test_plan_enforces_explicit_application_write_cap():
    plan = make_plan()
    assert len(plan.writes) == 4
    with pytest.raises(TransactionWriteLimitExceeded, match="application cap 3"):
        make_plan(max_writes=3)


def test_plan_rejects_wrong_household_and_non_persistable_price_fields():
    with pytest.raises(FirestoreMappingError, match="household scope"):
        build_approved_transaction_plan(
            household_id=HOME,
            operations_by_id={
                "op-1": (
                    CreateDocument(
                        {
                            "id": "transaction:tx-1",
                            "record_type": "transaction",
                            "household_id": "other",
                        }
                    ),
                )
            },
            shared_operations=(),
            completed_approval=ReplaceDocument(
                {
                    "id": "pending_approval:approval-1",
                    "record_type": "pending_approval",
                    "household_id": HOME,
                    "approval_id": "approval-1",
                    "status": "completed",
                },
                if_match_etag="version",
            ),
            outcome_by_operation={"op-1": {}},
        )

    with pytest.raises(FirestoreMappingError, match="Price-related"):
        build_approved_transaction_plan(
            household_id=HOME,
            operations_by_id={
                "op-1": (
                    CreateDocument(
                        {
                            "id": "transaction:tx-1",
                            "record_type": "transaction",
                            "household_id": HOME,
                            "estimated_price": 99,
                        }
                    ),
                )
            },
            shared_operations=(),
            completed_approval=ReplaceDocument(
                {
                    "id": "pending_approval:approval-1",
                    "record_type": "pending_approval",
                    "household_id": HOME,
                    "approval_id": "approval-1",
                    "status": "completed",
                },
                if_match_etag="version",
            ),
            outcome_by_operation={"op-1": {}},
        )
