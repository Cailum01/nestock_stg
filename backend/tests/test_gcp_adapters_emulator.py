"""Provider-emulator coverage; enable with Firestore/GCS emulator environment variables."""

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.dependencies import _create_media_storage_service, _create_selected_composition
from app.models.domain import InventoryItem, MediaInputMapping, MediaProcessingStatus
from app.models.identity import (
    DeviceRecord,
    DeviceRecoveryRecord,
    HouseholdCreation,
    HouseholdInviteRecord,
    HouseholdMembership,
    HouseholdRecord,
    HouseholdRecoveryRecord,
    HouseholdRole,
)
from app.models.storage import AllowedMediaContentType, UploadAuthorizationRequest
from app.services.approvals import (
    ApprovalPresentation,
    ApprovalRecord,
    ApprovalStatus,
    StoredOperation,
)
from app.services.bootstrap_issuer import (
    BootstrapIssuerRateLimited,
    FirestoreBootstrapIssuerRepository,
)
from app.services.cosmos_read import CosmosReadRepository
from app.services.cosmos_write import (
    BatchOperationConflict,
    CreateDocument,
    OperationIdConflict,
    ReplaceDocument,
    WriteStatus,
)
from app.services.firestore_identity import FirestoreIdentityRepository, seed_bootstrap_challenges
from app.services.firestore_mapping import (
    FirestoreDocumentMapper,
    FirestoreTransactionWrite,
    TransactionWriteLimitExceeded,
)
from app.services.firestore_sdk import (
    MAX_TRANSACTION_WRITES,
    FirestoreDocumentQueryClient,
    FirestoreTransactionalBatchClient,
    FirestoreWriteRepository,
    preflight_firestore_writes,
)
from app.services.google_identity import (
    GoogleIdentityRateLimited,
    GoogleIdentityService,
)
from app.services.identity import (
    ChallengeRateLimited,
    HouseholdCreationRateLimited,
    InviteRateLimited,
    InviteRedemptionRejected,
    MembershipConflict,
    hash_secret,
)
from app.services.media_mapping import MediaMappingSnapshot

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
FIRESTORE_EMULATOR = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
STORAGE_EMULATOR = bool(os.environ.get("STORAGE_EMULATOR_HOST"))


def _client():
    from google.cloud import firestore

    return firestore.Client(project=f"nestock-test-{uuid.uuid4().hex}")


def test_explicit_provider_settings_fail_closed_before_sdk_use(monkeypatch):
    with pytest.raises(RuntimeError, match="requires complete NESTOCK_COSMOS"):
        _create_selected_composition({"NESTOCK_DATA_PROVIDER": "cosmos"})
    with pytest.raises(RuntimeError, match="require explicit NESTOCK_DATA_PROVIDER=firestore"):
        _create_selected_composition({"NESTOCK_GCS_PROJECT_ID": "local-project"})
    with pytest.raises(RuntimeError, match="missing NESTOCK_FIRESTORE_PROJECT_ID"):
        _create_selected_composition({"NESTOCK_DATA_PROVIDER": "firestore"})
    with pytest.raises(RuntimeError, match="GCS configuration is incomplete"):
        _create_media_storage_service(
            environment={
                "NESTOCK_DATA_PROVIDER": "firestore",
                "NESTOCK_MEDIA_PROVIDER": "gcs",
                "NESTOCK_GCS_BUCKET": "private-bucket",
            }
        )
    from google.cloud import storage

    class Bucket:
        def __init__(self, *, public_access_prevention: str):
            self.name = "private-bucket"
            self.iam_configuration = type(
                "IAMConfiguration",
                (),
                {
                    "uniform_bucket_level_access_enabled": True,
                    "public_access_prevention": public_access_prevention,
                },
            )()

        def reload(self):
            return None

    class Client:
        def __init__(self, bucket):
            self._bucket = bucket

        def bucket(self, name):
            assert name == self._bucket.name
            return self._bucket

        def close(self):
            return None

    settings = {
        "NESTOCK_DATA_PROVIDER": "firestore",
        "NESTOCK_MEDIA_PROVIDER": "gcs",
        "NESTOCK_GCS_PROJECT_ID": "local-project",
        "NESTOCK_GCS_BUCKET": "private-bucket",
        "NESTOCK_STAGING_BASE_URL": "https://nestock-stg-api.example/staging/upload",
    }
    monkeypatch.setattr(
        storage,
        "Client",
        lambda project: Client(Bucket(public_access_prevention="inherited")),
    )
    with pytest.raises(RuntimeError, match="public access prevention"):
        _create_media_storage_service(environment=settings, mapping_port=object())

    monkeypatch.setattr(
        storage,
        "Client",
        lambda project: Client(Bucket(public_access_prevention="enforced")),
    )
    from app.services.provisional_storage.gcs_adapter import GCSMediaStorage

    assert isinstance(
        _create_media_storage_service(environment=settings, mapping_port=object()),
        GCSMediaStorage,
    )


def test_firestore_transaction_preflight_uses_app_cap_and_conservative_size():
    oversized_writes = tuple(
        FirestoreTransactionWrite(
            path=f"households/h/records/r{index}",
            kind="create",
            document={"id": f"r{index}", "record_type": "test", "value": "x"},
        )
        for index in range(MAX_TRANSACTION_WRITES + 1)
    )
    with pytest.raises(TransactionWriteLimitExceeded, match="application cap"):
        preflight_firestore_writes(oversized_writes)

    oversized_document = FirestoreTransactionWrite(
        path="households/h/records/large",
        kind="create",
        document={"id": "large", "record_type": "test", "payload": "x" * 600_000},
    )
    with pytest.raises(ValueError, match="1 MiB preflight"):
        preflight_firestore_writes((oversized_document,))

    from google.cloud import firestore

    transform = FirestoreTransactionWrite(
        path="households/h/records/transform",
        kind="create",
        document={
            "id": "transform",
            "record_type": "test",
            "created_at": firestore.SERVER_TIMESTAMP,
        },
    )
    with pytest.raises(ValueError, match="canonical JSON"):
        preflight_firestore_writes((transform,))


def test_firestore_transaction_preflight_rejects_aggregate_request_size():
    writes = tuple(
        FirestoreTransactionWrite(
            path=f"households/h/records/aggregate-{index}",
            kind="create",
            document={
                "id": f"aggregate-{index}",
                "record_type": "test",
                "payload": "x" * 400_000,
            },
        )
        for index in range(11)
    )
    with pytest.raises(ValueError, match="serialized-request preflight"):
        preflight_firestore_writes(writes)


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_firestore_domain_mapping_is_household_scoped():
    client = _client()
    query = FirestoreDocumentQueryClient(client)
    batch = FirestoreTransactionalBatchClient(client)
    read_repository = CosmosReadRepository(query)
    household_id = f"home-{uuid.uuid4().hex}"
    item = InventoryItem(
        item_id=f"item-{uuid.uuid4().hex}",
        household_id=household_id,
        name="Rice",
        quantity=2,
        unit="bag",
        created_at=NOW,
        updated_at=NOW,
    )
    mapped = FirestoreDocumentMapper.to_document(item)

    async def execute():
        await batch.execute_batch(
            partition_key=household_id,
            operations=(CreateDocument(mapped.document),),
        )
        assert (
            await read_repository.read_inventory_item(
                household_id=household_id,
                item_id=item.item_id,
            )
            == item
        )
        assert (
            await read_repository.read_inventory_item(
                household_id=f"other-{uuid.uuid4().hex}",
                item_id=item.item_id,
            )
            is None
        )
        assert await read_repository.list_inventory(household_id) == [item]

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_firestore_atomic_approval_commit_idempotency_and_scope():
    client = _client()
    query = FirestoreDocumentQueryClient(client)
    batch = FirestoreTransactionalBatchClient(client)
    repository = FirestoreWriteRepository(
        client,
        query_client=query,
        transactional_client=batch,
    )
    household_id = f"home-{uuid.uuid4().hex}"
    operation_id = f"op-{uuid.uuid4().hex}"
    approval_id = f"approval-{uuid.uuid4().hex}"

    def approval(status=ApprovalStatus.PENDING, **updates):
        created = ApprovalRecord(
            approval_id=approval_id,
            household_id=household_id,
            presentation=ApprovalPresentation.SINGLE_ACTION,
            operations=[
                StoredOperation(
                    operation_id=operation_id,
                    action="test_write",
                    description="Test write",
                    values={"value": "approved"},
                )
            ],
            status=status,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            **updates,
        )
        return created

    async def execute():
        pending = approval()
        stored_pending = await repository.create_pending_approval(
            household_id=household_id,
            approval=pending.to_document(timedelta(days=1)),
            ttl_seconds=86_400,
        )
        duplicate_pending = await repository.create_pending_approval(
            household_id=household_id,
            approval=pending.to_document(timedelta(days=1)),
            ttl_seconds=86_400,
        )
        assert duplicate_pending.etag == stored_pending.etag
        confirmed = approval(
            ApprovalStatus.CONFIRMED,
            decided_at=NOW,
        )
        stored_confirmed = await repository.replace_pending_approval(
            household_id=household_id,
            approval_id=approval_id,
            document=confirmed.to_document(timedelta(days=1)),
            if_match_etag=stored_pending.etag,
        )
        completed = approval(
            ApprovalStatus.COMPLETED,
            decided_at=NOW,
            completed_at=NOW,
            outcome={operation_id: {"committed": True}},
        )
        new_record = {
            "id": f"inventory_item:{uuid.uuid4().hex}",
            "record_type": "inventory_item",
            "household_id": household_id,
            "item_id": uuid.uuid4().hex,
            "value": "committed",
        }
        first_status = await repository.execute_idempotent_batches(
            household_id=household_id,
            operations_by_id={operation_id: (CreateDocument(new_record),)},
            shared_operations=(
                ReplaceDocument(
                    completed.to_document(timedelta(days=1)),
                    if_match_etag=stored_confirmed.etag,
                ),
            ),
        )
        retry_status = await repository.execute_idempotent_batches(
            household_id=household_id,
            operations_by_id={operation_id: (CreateDocument(new_record),)},
            shared_operations=(
                ReplaceDocument(
                    completed.to_document(timedelta(days=1)),
                    if_match_etag=stored_confirmed.etag,
                ),
            ),
        )
        assert first_status is WriteStatus.APPLIED
        assert retry_status is WriteStatus.ALREADY_APPLIED
        saved = await query.read_item(
            item_id=str(new_record["id"]),
            partition_key=household_id,
        )
        assert saved is not None and saved.get("value") == "committed"

        other_household = f"home-{uuid.uuid4().hex}"
        other_doc = dict(new_record, household_id=other_household)
        other_completed = completed.model_copy(update={"household_id": other_household})
        with pytest.raises(OperationIdConflict):
            await repository.execute_idempotent_batches(
                household_id=other_household,
                operations_by_id={operation_id: (CreateDocument(other_doc),)},
                shared_operations=(
                    ReplaceDocument(
                        other_completed.to_document(timedelta(days=1)),
                        if_match_etag="unused",
                    ),
                ),
            )
        assert (
            await query.read_item(
                item_id=str(new_record["id"]),
                partition_key=other_household,
            )
            is None
        )

        # A stale replacement precondition aborts every write in the transaction.
        second_id = f"op-{uuid.uuid4().hex}"
        second_approval_id = f"approval-{uuid.uuid4().hex}"
        second_pending = ApprovalRecord(
            approval_id=second_approval_id,
            household_id=household_id,
            presentation=ApprovalPresentation.SINGLE_ACTION,
            operations=[
                StoredOperation(
                    operation_id=second_id,
                    action="test_write",
                    description="Test write",
                    values={"value": "approved"},
                )
            ],
            status=ApprovalStatus.PENDING,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        second_stored = await repository.create_pending_approval(
            household_id=household_id,
            approval=second_pending.to_document(timedelta(days=1)),
            ttl_seconds=86_400,
        )
        second_confirmed = second_pending.model_copy(
            update={"status": ApprovalStatus.CONFIRMED, "decided_at": NOW}
        )
        second_confirmed_stored = await repository.replace_pending_approval(
            household_id=household_id,
            approval_id=second_approval_id,
            document=second_confirmed.to_document(timedelta(days=1)),
            if_match_etag=second_stored.etag,
        )
        stale_target = {
            "id": f"inventory_item:{uuid.uuid4().hex}",
            "record_type": "inventory_item",
            "household_id": household_id,
            "item_id": uuid.uuid4().hex,
            "value": "before",
        }
        await batch.execute_batch(
            partition_key=household_id,
            operations=(CreateDocument(stale_target),),
        )
        create_before_conflict = {
            "id": f"inventory_item:{uuid.uuid4().hex}",
            "record_type": "inventory_item",
            "household_id": household_id,
            "item_id": uuid.uuid4().hex,
            "value": "must-not-commit",
        }
        second_completed = second_confirmed.model_copy(
            update={
                "status": ApprovalStatus.COMPLETED,
                "completed_at": NOW,
                "outcome": {second_id: {"committed": True}},
            }
        )
        with pytest.raises(BatchOperationConflict):
            await repository.execute_idempotent_batches(
                household_id=household_id,
                operations_by_id={
                    second_id: (
                        CreateDocument(create_before_conflict),
                        ReplaceDocument(stale_target, if_match_etag="stale-version"),
                    )
                },
                shared_operations=(
                    ReplaceDocument(
                        second_completed.to_document(timedelta(days=1)),
                        if_match_etag=second_confirmed_stored.etag,
                    ),
                ),
            )
        assert (
            await query.read_item(
                item_id=str(create_before_conflict["id"]),
                partition_key=household_id,
            )
            is None
        )
        claim = await repository.read_operation_receipt(
            household_id=household_id,
            operation_id=second_id,
        )
        assert claim is None
        current_approval = await repository.read_pending_approval(
            household_id=household_id,
            approval_id=second_approval_id,
        )
        assert current_approval is not None
        assert current_approval.document["status"] == ApprovalStatus.CONFIRMED.value

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_firestore_concurrent_invite_redemption_is_single_use():
    client = _client()
    repository = FirestoreIdentityRepository(client)
    identity = f"test-{uuid.uuid4().hex}"
    owner_id = f"owner-{uuid.uuid4().hex}"
    first_member_id = f"member-{uuid.uuid4().hex}"
    second_member_id = f"member-{uuid.uuid4().hex}"
    household_id = f"home-{uuid.uuid4().hex}"
    challenge_hashes = [hash_secret(f"{identity}-challenge-{index}") for index in range(3)]
    seed_bootstrap_challenges(client, challenge_hashes, now=NOW)

    def device(device_id: str) -> DeviceRecord:
        return DeviceRecord(
            device_id=device_id,
            credential_hash=hash_secret(f"{identity}-{device_id}"),
            created_at=NOW,
        )

    owner = device(owner_id)
    first_member = device(first_member_id)
    second_member = device(second_member_id)

    async def execute():
        for challenge_hash, record in zip(
            challenge_hashes,
            (owner, first_member, second_member),
            strict=True,
        ):
            assert await repository.bootstrap_install(
                challenge_hash=challenge_hash,
                device=record,
                now=NOW,
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        seed_bootstrap_challenges(client, (challenge_hashes[0],))
        assert not await repository.bootstrap_install(
            challenge_hash=challenge_hashes[0],
            device=device(f"replay-{uuid.uuid4().hex}"),
            now=NOW,
            failure_limit=5,
            failure_window=timedelta(minutes=15),
            cooldown=timedelta(minutes=15),
        )
        failed_challenge_hash = hash_secret(f"{identity}-missing-bootstrap-challenge")
        throttled_repository = FirestoreIdentityRepository(client)
        for index in range(8):
            assert not await throttled_repository.bootstrap_install(
                challenge_hash=failed_challenge_hash,
                device=device(f"failed-bootstrap-{index}-{uuid.uuid4().hex}"),
                now=NOW + timedelta(seconds=index),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        assert (
            client.document(f"bootstrap_challenges/{failed_challenge_hash}").get().exists is False
        )
        creation = HouseholdCreation(
            household=HouseholdRecord(
                household_id=household_id,
                created_by_device_id=owner_id,
                created_at=NOW,
            ),
            owner_membership=HouseholdMembership(
                household_id=household_id,
                device_id=owner_id,
                role=HouseholdRole.OWNER,
                created_at=NOW,
            ),
        )
        await repository.create_household(
            device_id=owner_id,
            creation=creation,
            now=NOW,
            max_per_hour=3,
            window=timedelta(hours=1),
        )
        for index in range(4):
            with pytest.raises(InviteRedemptionRejected):
                await repository.redeem_device_recovery(
                    code_hash=hash_secret(f"{identity}-missing-recovery-{index}"),
                    now=NOW + timedelta(seconds=index),
                    failure_limit=5,
                    failure_window=timedelta(minutes=15),
                    cooldown=timedelta(minutes=15),
                )
        with pytest.raises(InviteRedemptionRejected):
            await repository.redeem_device_recovery(
                code_hash=hash_secret(f"{identity}-missing-recovery-final"),
                now=NOW + timedelta(seconds=4),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        with pytest.raises(InviteRedemptionRejected):
            await FirestoreIdentityRepository(client).redeem_device_recovery(
                code_hash=hash_secret(f"{identity}-missing-recovery-after-restart"),
                now=NOW + timedelta(seconds=5),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        assert (await repository.list_device_memberships(owner_id)).memberships == (
            creation.owner_membership,
        )
        invite_code = f"{identity}-one-use-invite"
        invite = HouseholdInviteRecord(
            invite_id=f"invite-{uuid.uuid4().hex}",
            household_id=household_id,
            token_hash=hash_secret(invite_code),
            created_by_device_id=owner_id,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        await repository.create_invite(
            actor_device_id=owner_id,
            invite=invite,
            now=NOW,
            max_per_hour=10,
        )

        async def redeem(member_id: str):
            try:
                return await repository.redeem_invite(
                    token_hash=invite.token_hash,
                    device_id=member_id,
                    network_key=hash_secret(f"{identity}-{member_id}"),
                    now=NOW,
                    failure_limit=5,
                    failure_window=timedelta(minutes=15),
                    cooldown=timedelta(minutes=15),
                )
            except InviteRedemptionRejected as error:
                return error

        async def simultaneous():
            return await asyncio.gather(redeem(first_member_id), redeem(second_member_id))

        outcomes = await simultaneous()
        succeeded = [item for item in outcomes if isinstance(item, HouseholdMembership)]
        failed = [item for item in outcomes if isinstance(item, InviteRedemptionRejected)]
        assert len(succeeded) == 1
        assert len(failed) == 1
        assert succeeded[0].household_id == household_id
        members = await repository.list_device_memberships(succeeded[0].device_id)
        assert [item.household_id for item in members.memberships] == [household_id]

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_firestore_household_recovery_failures_are_transactionally_rate_limited():
    client = _client()
    repository = FirestoreIdentityRepository(client)
    identity = f"test-{uuid.uuid4().hex}"
    owner_id = f"owner-{uuid.uuid4().hex}"
    replacement_id = f"replacement-{uuid.uuid4().hex}"
    household_id = f"home-{uuid.uuid4().hex}"
    owner_challenge = hash_secret(f"{identity}-owner-challenge")
    replacement_challenge = hash_secret(f"{identity}-replacement-challenge")
    seed_bootstrap_challenges(client, (owner_challenge, replacement_challenge), now=NOW)

    def device(device_id: str) -> DeviceRecord:
        return DeviceRecord(
            device_id=device_id,
            credential_hash=hash_secret(f"{identity}-{device_id}"),
            created_at=NOW,
        )

    async def execute():
        owner = device(owner_id)
        replacement = device(replacement_id)
        for challenge_hash, record in (
            (owner_challenge, owner),
            (replacement_challenge, replacement),
        ):
            assert await repository.bootstrap_install(
                challenge_hash=challenge_hash,
                device=record,
                now=NOW,
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )

        await repository.create_household(
            device_id=owner_id,
            creation=HouseholdCreation(
                household=HouseholdRecord(
                    household_id=household_id,
                    created_by_device_id=owner_id,
                    created_at=NOW,
                ),
                owner_membership=HouseholdMembership(
                    household_id=household_id,
                    device_id=owner_id,
                    role=HouseholdRole.OWNER,
                    created_at=NOW,
                ),
            ),
            now=NOW,
            max_per_hour=3,
            window=timedelta(hours=1),
        )
        recovery_code = f"{identity}-household-recovery"
        await repository.issue_household_recovery(
            actor_device_id=owner_id,
            recovery=HouseholdRecoveryRecord(
                recovery_id=f"recovery-{uuid.uuid4().hex}",
                household_id=household_id,
                code_hash=hash_secret(recovery_code),
                created_at=NOW,
            ),
        )
        device_recovery_code = f"{identity}-device-recovery"
        await repository.issue_device_recovery(
            actor_device_id=owner_id,
            recovery=DeviceRecoveryRecord(
                recovery_id=f"device-recovery-{uuid.uuid4().hex}",
                device_id=owner_id,
                code_hash=hash_secret(device_recovery_code),
                created_at=NOW,
            ),
        )
        await repository.redeem_device_recovery(
            code_hash=hash_secret(device_recovery_code),
            now=NOW,
            failure_limit=5,
            failure_window=timedelta(minutes=15),
            cooldown=timedelta(minutes=15),
        )

        for index in range(4):
            with pytest.raises(InviteRedemptionRejected):
                await repository.redeem_household_recovery(
                    code_hash=hash_secret(recovery_code),
                    device_id=f"missing-{replacement_id}",
                    now=NOW + timedelta(seconds=index),
                    failure_limit=5,
                    failure_window=timedelta(minutes=15),
                    cooldown=timedelta(minutes=15),
                )
        with pytest.raises(ChallengeRateLimited):
            await repository.redeem_household_recovery(
                code_hash=hash_secret(recovery_code),
                device_id=f"missing-{replacement_id}",
                now=NOW + timedelta(seconds=4),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        assert (await repository.list_device_memberships(replacement_id)).memberships == ()
        with pytest.raises(ChallengeRateLimited):
            await repository.redeem_household_recovery(
                code_hash=hash_secret(recovery_code),
                device_id=replacement_id,
                now=NOW + timedelta(seconds=5),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        assert (await repository.list_device_memberships(replacement_id)).memberships == ()

        membership = await repository.redeem_household_recovery(
            code_hash=hash_secret(recovery_code),
            device_id=replacement_id,
            # The fifth failure starts the cooldown at t+4s; wait beyond that
            # full window before checking that a valid recovery can be retried.
            now=NOW + timedelta(minutes=15, seconds=5),
            failure_limit=5,
            failure_window=timedelta(minutes=15),
            cooldown=timedelta(minutes=15),
        )
        assert membership.household_id == household_id
        assert membership.role is HouseholdRole.OWNER
        assert (await repository.list_device_memberships(replacement_id)).memberships == (
            membership,
        )
        with pytest.raises(InviteRedemptionRejected):
            await repository.redeem_household_recovery(
                code_hash=hash_secret(recovery_code),
                device_id=replacement_id,
                now=NOW + timedelta(minutes=15, seconds=6),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_firestore_concurrent_household_creation_respects_per_install_limit():
    client = _client()
    repository = FirestoreIdentityRepository(client)
    identity = f"test-{uuid.uuid4().hex}"
    owner_id = f"owner-{uuid.uuid4().hex}"
    challenge_hash = hash_secret(f"{identity}-owner-challenge")
    seed_bootstrap_challenges(client, (challenge_hash,), now=NOW)
    attempts = [f"home-{uuid.uuid4().hex}" for _ in range(8)]

    owner = DeviceRecord(
        device_id=owner_id,
        credential_hash=hash_secret(f"{identity}-{owner_id}"),
        created_at=NOW,
    )

    async def create(household_id: str) -> str:
        await repository.create_household(
            device_id=owner_id,
            creation=HouseholdCreation(
                household=HouseholdRecord(
                    household_id=household_id,
                    created_by_device_id=owner_id,
                    created_at=NOW,
                ),
                owner_membership=HouseholdMembership(
                    household_id=household_id,
                    device_id=owner_id,
                    role=HouseholdRole.OWNER,
                    created_at=NOW,
                ),
            ),
            now=NOW,
            max_per_hour=3,
            window=timedelta(hours=1),
        )
        return household_id

    async def execute() -> None:
        assert await repository.bootstrap_install(
            challenge_hash=challenge_hash,
            device=owner,
            now=NOW,
            failure_limit=5,
            failure_window=timedelta(minutes=15),
            cooldown=timedelta(minutes=15),
        )

        outcomes = await asyncio.gather(
            *(create(household_id) for household_id in attempts),
            return_exceptions=True,
        )
        created = [outcome for outcome in outcomes if isinstance(outcome, str)]
        rejected = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]

        assert len(created) == 3
        assert len(rejected) == len(attempts) - 3
        assert all(isinstance(outcome, HouseholdCreationRateLimited) for outcome in rejected)
        assert len((await repository.list_device_memberships(owner_id)).memberships) == 3
        assert (
            sum(
                client.document(f"households/{household_id}").get().exists
                for household_id in attempts
            )
            == 3
        )
        limit = (
            client.document(f"identity_attempt_limits/household-create-{owner_id}").get().to_dict()
        )
        assert len(limit["created_at"]) == 3

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_google_identity_reinstall_restores_same_default_household():
    client = _client()
    repository = FirestoreIdentityRepository(client)
    subject = f"google-subject-{uuid.uuid4().hex}"
    audience = "test-web-client"

    def verify(_token: str, expected_audience: str) -> dict[str, object]:
        return {
            "iss": "accounts.google.com",
            "aud": expected_audience,
            "exp": datetime.now(UTC).timestamp() + 60,
            "sub": subject,
        }

    service = GoogleIdentityService(repository, audience=audience, verifier=verify)

    async def execute() -> None:
        first_device, first_bearer, first_household = await service.authenticate("google-id-token")
        second_device, second_bearer, second_household = await service.authenticate(
            "google-id-token"
        )
        third_device, third_bearer, third_household = await service.authenticate(
            "google-id-token"
        )

        assert first_device != second_device
        assert second_device != third_device
        assert first_bearer != second_bearer
        assert second_bearer != third_bearer
        assert first_household == second_household
        assert second_household == third_household
        with pytest.raises(GoogleIdentityRateLimited):
            await service.authenticate("google-id-token")
        first_memberships = await repository.list_device_memberships(first_device)
        second_memberships = await repository.list_device_memberships(second_device)
        third_memberships = await repository.list_device_memberships(third_device)
        assert first_memberships.memberships[0].role is HouseholdRole.OWNER
        assert second_memberships.memberships[0].role is HouseholdRole.MEMBER
        assert third_memberships.memberships[0].role is HouseholdRole.MEMBER
        mapping = client.document(f"google_identities/{hash_secret(subject)}").get().to_dict()
        assert mapping["default_household_id"] == first_household
        assert "sub" not in mapping
        limit = client.document(
            f"identity_attempt_limits/google-install-{hash_secret(subject)}"
        ).get().to_dict()
        assert len(limit["created_at"]) == 3

        for device_id in (first_device, second_device, third_device):
            extra_household_id = f"extra-{uuid.uuid4().hex}"
            created_at = datetime.now(UTC)
            with pytest.raises(MembershipConflict):
                await repository.create_household(
                    device_id=device_id,
                    creation=HouseholdCreation(
                        household=HouseholdRecord(
                            household_id=extra_household_id,
                            created_by_device_id=device_id,
                            created_at=created_at,
                        ),
                        owner_membership=HouseholdMembership(
                            household_id=extra_household_id,
                            device_id=device_id,
                            role=HouseholdRole.OWNER,
                            created_at=created_at,
                        ),
                    ),
                    now=created_at,
                    max_per_hour=3,
                    window=timedelta(hours=1),
                )
            assert not client.document(f"households/{extra_household_id}").get().exists

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_firestore_household_creation_limit_is_per_install_and_expires():
    client = _client()
    repository = FirestoreIdentityRepository(client)
    identity = f"test-{uuid.uuid4().hex}"
    owner_id = f"owner-{uuid.uuid4().hex}"
    other_id = f"other-{uuid.uuid4().hex}"
    challenges = (
        hash_secret(f"{identity}-owner-challenge"),
        hash_secret(f"{identity}-other-challenge"),
    )
    seed_bootstrap_challenges(client, challenges, now=NOW)

    def device(device_id: str) -> DeviceRecord:
        return DeviceRecord(
            device_id=device_id,
            credential_hash=hash_secret(f"{identity}-{device_id}"),
            created_at=NOW,
        )

    async def create(device_id: str, now: datetime) -> None:
        household_id = f"home-{uuid.uuid4().hex}"
        await repository.create_household(
            device_id=device_id,
            creation=HouseholdCreation(
                household=HouseholdRecord(
                    household_id=household_id,
                    created_by_device_id=device_id,
                    created_at=now,
                ),
                owner_membership=HouseholdMembership(
                    household_id=household_id,
                    device_id=device_id,
                    role=HouseholdRole.OWNER,
                    created_at=now,
                ),
            ),
            now=now,
            max_per_hour=3,
            window=timedelta(hours=1),
        )

    async def execute() -> None:
        for challenge_hash, record in zip(
            challenges, (device(owner_id), device(other_id)), strict=True
        ):
            assert await repository.bootstrap_install(
                challenge_hash=challenge_hash,
                device=record,
                now=NOW,
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )

        for _ in range(3):
            await create(owner_id, NOW)
        with pytest.raises(HouseholdCreationRateLimited):
            await create(owner_id, NOW)
        assert len((await repository.list_device_memberships(owner_id)).memberships) == 3

        # A separate install is independent, and the same install recovers
        # when its rolling window has elapsed.
        await create(other_id, NOW)
        await create(owner_id, NOW + timedelta(hours=1, seconds=1))
        assert len((await repository.list_device_memberships(owner_id)).memberships) == 4
        limit = (
            client.document(f"identity_attempt_limits/household-create-{owner_id}").get().to_dict()
        )
        assert isinstance(limit["expires_at"], datetime)

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_firestore_identity_expiry_metadata_and_bootstrap_lifecycle():
    client = _client()
    repository = FirestoreIdentityRepository(client)
    issuer_repository = FirestoreBootstrapIssuerRepository(client)
    identity = f"test-{uuid.uuid4().hex}"
    owner_id = f"owner-{uuid.uuid4().hex}"
    household_id = f"home-{uuid.uuid4().hex}"
    valid_challenge = hash_secret(f"{identity}-valid-bootstrap")
    expired_challenge = hash_secret(f"{identity}-expired-bootstrap")
    seed_bootstrap_challenges(
        client,
        (valid_challenge, expired_challenge),
        now=NOW - timedelta(minutes=16),
    )
    # Replace the valid fixture with an explicit unexpired Timestamp document.
    client.document(f"bootstrap_challenges/{valid_challenge}").set(
        {
            "id": valid_challenge,
            "record_type": "bootstrap_challenge",
            "created_at": NOW,
            "expires_at": NOW + timedelta(minutes=15),
        }
    )

    def device(device_id: str) -> DeviceRecord:
        return DeviceRecord(
            device_id=device_id,
            credential_hash=hash_secret(f"{identity}-{device_id}"),
            created_at=NOW,
        )

    async def execute():
        challenge_documents = [
            client.document(f"bootstrap_challenges/{item}").get().to_dict()
            for item in (valid_challenge, expired_challenge)
        ]
        assert all(isinstance(item["expires_at"], datetime) for item in challenge_documents)
        assert challenge_documents[0]["expires_at"] > NOW
        assert challenge_documents[1]["expires_at"] < NOW

        assert not await repository.bootstrap_install(
            challenge_hash=expired_challenge,
            device=device(f"expired-{uuid.uuid4().hex}"),
            now=NOW,
            failure_limit=5,
            failure_window=timedelta(minutes=15),
            cooldown=timedelta(minutes=15),
        )
        owner = device(owner_id)
        assert await repository.bootstrap_install(
            challenge_hash=valid_challenge,
            device=owner,
            now=NOW,
            failure_limit=5,
            failure_window=timedelta(minutes=15),
            cooldown=timedelta(minutes=15),
        )
        assert not await repository.bootstrap_install(
            challenge_hash=valid_challenge,
            device=device(f"replay-{uuid.uuid4().hex}"),
            now=NOW + timedelta(seconds=1),
            failure_limit=5,
            failure_window=timedelta(minutes=15),
            cooldown=timedelta(minutes=15),
        )

        for index in range(3):
            assert not await repository.bootstrap_install(
                challenge_hash=expired_challenge,
                device=device(f"expired-retry-{index}-{uuid.uuid4().hex}"),
                now=NOW + timedelta(seconds=index + 1),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        with pytest.raises(ChallengeRateLimited):
            await repository.bootstrap_install(
                challenge_hash=expired_challenge,
                device=device(f"expired-retry-final-{uuid.uuid4().hex}"),
                now=NOW + timedelta(seconds=4),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        expired_document = (
            client.document(f"bootstrap_challenges/{expired_challenge}").get().to_dict()
        )
        assert len(expired_document["failed_at"]) == 5
        assert expired_document["expires_at"] == NOW - timedelta(minutes=1)

        random_hashes = [hash_secret(f"{identity}-random-{index}") for index in range(8)]
        for index, random_hash in enumerate(random_hashes):
            assert not await repository.bootstrap_install(
                challenge_hash=random_hash,
                device=device(f"random-{index}-{uuid.uuid4().hex}"),
                now=NOW + timedelta(seconds=index),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        assert all(
            not client.document(f"bootstrap_challenges/{item}").get().exists
            for item in random_hashes
        )

        await repository.create_household(
            device_id=owner_id,
            creation=HouseholdCreation(
                household=HouseholdRecord(
                    household_id=household_id,
                    created_by_device_id=owner_id,
                    created_at=NOW,
                ),
                owner_membership=HouseholdMembership(
                    household_id=household_id,
                    device_id=owner_id,
                    role=HouseholdRole.OWNER,
                    created_at=NOW,
                ),
            ),
            now=NOW,
            max_per_hour=3,
            window=timedelta(hours=1),
        )
        invite = HouseholdInviteRecord(
            invite_id=f"invite-{uuid.uuid4().hex}",
            household_id=household_id,
            token_hash=hash_secret(f"{identity}-invite"),
            created_by_device_id=owner_id,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        await repository.create_invite(
            actor_device_id=owner_id,
            invite=invite,
            now=NOW,
            max_per_hour=10,
        )
        invite_limit = client.document(f"invite_limits/{household_id}").get().to_dict()
        assert isinstance(invite_limit["expires_at"], datetime)
        assert invite_limit["expires_at"] == NOW + timedelta(hours=1)

        network_key = "emulator-test-peer"
        invite_rate_digest = hashlib.sha256(f"{owner_id}\0{network_key}".encode()).hexdigest()
        for index in range(5):
            redeem = repository.redeem_invite(
                token_hash=hash_secret(f"{identity}-bad-invite-{index}"),
                device_id=owner_id,
                network_key=network_key,
                now=NOW + timedelta(seconds=index),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
            if index < 4:
                with pytest.raises(InviteRedemptionRejected):
                    await redeem
            else:
                with pytest.raises(InviteRateLimited):
                    await redeem
        invite_attempt = client.document(f"invite_attempts/{invite_rate_digest}").get().to_dict()
        assert isinstance(invite_attempt["expires_at"], datetime)
        assert invite_attempt["expires_at"] >= datetime.fromisoformat(
            invite_attempt["cooldown_until"].replace("Z", "+00:00")
        )
        with pytest.raises(InviteRateLimited):
            await repository.redeem_invite(
                token_hash=invite.token_hash,
                device_id=owner_id,
                network_key=network_key,
                now=NOW + timedelta(seconds=6),
                failure_limit=5,
                failure_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )

        operator_key = hash_secret(f"{identity}-operator")
        for index in range(5):
            await issuer_repository.create_bootstrap_challenge(
                operator_key=operator_key,
                challenge_hash=hash_secret(f"{identity}-operator-challenge-{index}"),
                now=NOW + timedelta(seconds=index),
                expires_at=NOW + timedelta(seconds=index, minutes=15),
                issuance_limit=5,
                issuance_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        with pytest.raises(BootstrapIssuerRateLimited):
            await issuer_repository.create_bootstrap_challenge(
                operator_key=operator_key,
                challenge_hash=hash_secret(f"{identity}-operator-challenge-limited"),
                now=NOW + timedelta(seconds=5),
                expires_at=NOW + timedelta(minutes=15, seconds=5),
                issuance_limit=5,
                issuance_window=timedelta(minutes=15),
                cooldown=timedelta(minutes=15),
            )
        issuer_limit = client.document(f"identity_attempt_limits/{operator_key}").get().to_dict()
        assert isinstance(issuer_limit["expires_at"], datetime)
        issuer_challenge = (
            client.document(
                f"bootstrap_challenges/{hash_secret(f'{identity}-operator-challenge-0')}"
            )
            .get()
            .to_dict()
        )
        assert isinstance(issuer_challenge["expires_at"], datetime)
        assert issuer_challenge["expires_at"] == NOW + timedelta(minutes=15)

    try:
        asyncio.run(execute())
    finally:
        client.close()


@pytest.mark.skipif(not STORAGE_EMULATOR, reason="requires STORAGE_EMULATOR_HOST")
def test_gcs_media_is_private_server_mediated_and_owner_verified():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    project = f"nestock-test-{uuid.uuid4().hex}"
    client = storage.Client(project=project, credentials=AnonymousCredentials())
    bucket = client.bucket(f"nestock-media-{uuid.uuid4().hex}")
    bucket.create()
    household_id = f"home-{uuid.uuid4().hex}"
    from app.services.provisional_storage.gcs_adapter import GCSMediaStorage

    storage_service = GCSMediaStorage(
        bucket=bucket,
        base_staging_url="https://api.example.test/staging/upload",
        auto_process=False,
    )
    request = UploadAuthorizationRequest(
        household_id=household_id,
        filename="private.jpg",
        content_type=AllowedMediaContentType.IMAGE_JPEG,
        file_size_bytes=4,
    )
    upload = asyncio.run(storage_service.create_upload_authorization(request))
    assert upload.upload_url.startswith("https://api.example.test/staging/upload/")
    assert "storage.googleapis.com" not in upload.upload_url
    token = upload.upload_url.rsplit("/", 1)[-1]
    _, image_ref = asyncio.run(storage_service.receive_staged_upload(token, b"jpeg"))
    assert asyncio.run(storage_service.get_media_bytes(household_id, image_ref)) == b"jpeg"
    with pytest.raises(ValueError, match="authorized household"):
        asyncio.run(storage_service.get_media_bytes("other-household", image_ref))

    input_id = image_ref.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    record = MediaInputMapping(
        input_id=input_id,
        household_id=household_id,
        image_ref=image_ref,
        content_type="image/jpeg",
        expected_file_size_bytes=4,
        file_size_bytes=4,
        sha256_hash=hashlib.sha256(b"jpeg").hexdigest(),
        status=MediaProcessingStatus.COMPLETED,
        upload_authorization_expires_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        updated_at=NOW,
        artifact_ref=f"blob://households/{household_id}/media/{input_id}/derived/vision_v1.json",
    )

    class ReadyMapping:
        async def find_by_image_ref(self, *, household_id, image_ref):
            if household_id != record.household_id or image_ref != record.image_ref:
                return None
            return MediaMappingSnapshot(record=record, etag="emulator-version")

        async def find_completed_by_image_ref(self, *, household_id, image_ref):
            if household_id != record.household_id or image_ref != record.image_ref:
                return None
            return MediaMappingSnapshot(record=record, etag="emulator-version")

    storage_service._mapping_port = ReadyMapping()
    assert asyncio.run(storage_service.verify_media_exists(household_id, image_ref))
    assert asyncio.run(storage_service.retain_media(household_id, image_ref))
    assert (
        asyncio.run(
            storage_service.cleanup_expired_blobs(now=datetime.now(UTC) + timedelta(days=2))
        )
        == []
    )
    assert asyncio.run(storage_service.get_media_bytes(household_id, image_ref)) == b"jpeg"
    assert asyncio.run(storage_service.unretain_media(household_id, image_ref, ttl_seconds=1))
    cleaned = asyncio.run(
        storage_service.cleanup_expired_blobs(now=datetime.now(UTC) + timedelta(seconds=2))
    )
    assert image_ref in cleaned
    assert asyncio.run(storage_service.get_media_bytes(household_id, image_ref)) is None

    # A tampered ownership label cannot make the object visible to the owner.
    # Restore a fresh upload so the metadata check is exercised on an existing blob.
    storage_service._mapping_port = None
    second_upload = asyncio.run(storage_service.create_upload_authorization(request))
    second_token = second_upload.upload_url.rsplit("/", 1)[-1]
    _, second_ref = asyncio.run(storage_service.receive_staged_upload(second_token, b"jpeg"))
    second_input_id = second_ref.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    second_record = record.model_copy(
        update={
            "input_id": second_input_id,
            "image_ref": second_ref,
            "artifact_ref": (
                f"blob://households/{household_id}/media/{second_input_id}/derived/vision_v1.json"
            ),
        }
    )

    class SecondReadyMapping:
        async def find_by_image_ref(self, *, household_id, image_ref):
            if household_id != second_record.household_id or image_ref != second_record.image_ref:
                return None
            return MediaMappingSnapshot(record=second_record, etag="emulator-version")

        async def find_completed_by_image_ref(self, *, household_id, image_ref):
            if household_id != second_record.household_id or image_ref != second_record.image_ref:
                return None
            return MediaMappingSnapshot(record=second_record, etag="emulator-version")

    storage_service._mapping_port = SecondReadyMapping()
    assert asyncio.run(storage_service.verify_media_exists(household_id, second_ref))
    second_filename = second_ref.rsplit("/", 1)[-1]
    second_blob = bucket.blob(f"households/{household_id}/staging/{second_filename}")
    second_blob.reload()
    second_blob.metadata = {
        **(second_blob.metadata or {}),
        "household_id": "other-household",
    }
    second_blob.patch()
    assert not asyncio.run(storage_service.verify_media_exists(household_id, second_ref))
    assert asyncio.run(storage_service.get_media_bytes(household_id, second_ref)) is None
    client.close()
