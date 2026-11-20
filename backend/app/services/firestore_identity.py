"""Atomic Firestore implementation of the identity repository port."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1 import FieldFilter

from app.models.identity import (
    DeviceMemberships,
    DeviceRecord,
    DeviceRecoveryRecord,
    HouseholdCreation,
    HouseholdInviteRecord,
    HouseholdMembership,
    HouseholdRecoveryRecord,
    HouseholdRole,
)
from app.services.firestore_mapping import FirestoreDocumentMapper
from app.services.firestore_sdk import (
    _run_transaction,
    _single_snapshot,
)
from app.services.google_identity import GoogleIdentityRateLimited
from app.services.identity import (
    ChallengeRateLimited,
    ForbiddenIdentityAction,
    HouseholdCreationRateLimited,
    IdentityRepository,
    InviteRateLimited,
    InviteRedemptionRejected,
    MembershipConflict,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Firestore identity timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        try:
            return _utc(value)
        except ValueError:
            return None
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _read_model(
    model_type: type[Any],
    path: str,
    snapshot: Any,
) -> Any | None:
    if snapshot is None or not snapshot.exists:
        return None
    document = dict(snapshot.to_dict() or {})
    return FirestoreDocumentMapper.from_document(
        model_type,
        path=path,
        document=document,
    )


def _membership_write(
    membership: HouseholdMembership,
) -> tuple[Any, dict[str, object]]:
    mapped = FirestoreDocumentMapper.to_document(membership)
    document = dict(mapped.document)
    document["status"] = "revoked" if membership.revoked_at is not None else "active"
    return mapped, document


def _membership_reference(client: Any, device_id: str, household_id: str) -> Any:
    record = HouseholdMembership(
        device_id=device_id,
        household_id=household_id,
        role=HouseholdRole.MEMBER,
        created_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    mapped = FirestoreDocumentMapper.to_document(record)
    return client.document(mapped.path)


def _membership_from_snapshot(
    snapshot: Any,
    *,
    household_id: str | None = None,
) -> HouseholdMembership | None:
    if not snapshot or not snapshot.exists:
        return None
    document = dict(snapshot.to_dict() or {})
    status = document.pop("status", None)
    if status not in {"active", "revoked"}:
        raise ValueError("Stored Firestore membership has no valid status.")
    if (status == "active") != (document.get("revoked_at") is None):
        raise ValueError("Stored Firestore membership status is inconsistent.")
    return FirestoreDocumentMapper.from_document(
        HouseholdMembership,
        path=snapshot.reference.path,
        document=document,
        household_id=household_id,
    )


def _identity_write(record: object) -> tuple[Any, dict[str, object]]:
    mapped = FirestoreDocumentMapper.to_document(record)
    return mapped, dict(mapped.document)


def _challenge_failure_state(
    document: dict[str, Any],
    *,
    now: datetime,
    failure_limit: int,
    failure_window: timedelta,
    cooldown: timedelta,
) -> tuple[dict[str, object], bool]:
    """Return bounded failure state and whether this attempt starts a cooldown."""
    cutoff = _utc(now) - failure_window
    failures: list[str] = []
    for value in document.get("failed_at", []):
        if not isinstance(value, str):
            continue
        failed_at = _parse_timestamp(value)
        if failed_at is not None and failed_at > cutoff:
            failures.append(value)
    failures.append(_iso(now))
    limited = len(failures) >= failure_limit
    fields: dict[str, object] = {
        "failed_at": failures,
    }
    if limited:
        fields["cooldown_until"] = _iso(_utc(now) + cooldown)
    return fields, limited


def _cooldown_active(document: dict[str, Any], now: datetime) -> bool:
    value = document.get("cooldown_until")
    if not isinstance(value, str):
        return False
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00"))) > _utc(now)
    except ValueError:
        return False


def _invite_failure_state(
    document: dict[str, Any],
    *,
    now: datetime,
    failure_limit: int,
    failure_window: timedelta,
    cooldown: timedelta,
) -> tuple[dict[str, object], bool]:
    cutoff = _utc(now) - failure_window
    failures: list[str] = []
    for value in document.get("failed_at", []):
        if not isinstance(value, str):
            continue
        failed_at = _parse_timestamp(value)
        if failed_at is not None and failed_at > cutoff:
            failures.append(value)
    failures.append(_iso(now))
    limited = len(failures) >= failure_limit
    latest_failure = max(_parse_timestamp(value) or _utc(now) for value in failures)
    fields: dict[str, object] = {"failed_at": failures}
    expiry_candidates = [latest_failure + failure_window]
    if limited:
        cooldown_until = _utc(now) + cooldown
        fields["cooldown_until"] = _iso(cooldown_until)
        expiry_candidates.append(cooldown_until)
    fields["expires_at"] = max(expiry_candidates)
    return fields, limited


def _failure_state_expiry(
    document: dict[str, Any],
    *,
    now: datetime,
    failure_window: timedelta,
) -> datetime:
    candidates: list[datetime] = []
    for value in document.get("failed_at", []):
        parsed = _parse_timestamp(value)
        if parsed is not None:
            candidates.append(parsed + failure_window)
    cooldown_until = _parse_timestamp(document.get("cooldown_until"))
    if cooldown_until is not None:
        candidates.append(cooldown_until)
    existing_expiry = _parse_timestamp(document.get("expires_at"))
    if existing_expiry is not None:
        candidates.append(existing_expiry)
    return max(candidates, default=_utc(now) + failure_window)


def seed_bootstrap_challenges(
    client: Any,
    challenge_hashes: Sequence[str],
    *,
    now: datetime | None = None,
) -> None:
    """Create one-use bootstrap challenge documents from SHA-256 hashes only."""
    for challenge_hash in challenge_hashes:
        if len(challenge_hash) != 64 or any(
            character not in "0123456789abcdef" for character in challenge_hash
        ):
            raise RuntimeError(
                "NESTOCK_BOOTSTRAP_CHALLENGE_HASHES must contain SHA-256 hex hashes."
            )
    for challenge_hash in challenge_hashes:
        reference = client.document(f"bootstrap_challenges/{challenge_hash}")
        created_at = _utc(now) if now is not None else datetime.now(UTC)
        try:
            reference.create(
                {
                    "id": challenge_hash,
                    "record_type": "bootstrap_challenge",
                    "created_at": _iso(created_at),
                    "expires_at": created_at + timedelta(minutes=15),
                }
            )
        except AlreadyExists:
            snapshot = reference.get()
            document = dict(snapshot.to_dict() or {})
            stored_expiry = document.get("expires_at")
            if not isinstance(stored_expiry, datetime):
                created_at = _parse_timestamp(document.get("created_at"))
                expiry = (
                    created_at + timedelta(minutes=15)
                    if created_at is not None
                    else datetime.now(UTC) - timedelta(microseconds=1)
                )
                reference.update({"expires_at": expiry})


class FirestoreIdentityRepository(IdentityRepository):
    """Use Firestore transactions for each identity mutation and its preconditions."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def provision_google_install(
        self,
        *,
        subject_hash: str,
        device: DeviceRecord,
        now: datetime,
        max_per_hour: int,
        window: timedelta,
    ) -> None:
        """Atomically persist a verified Google install without household access."""
        if len(subject_hash) != 64 or any(
            character not in "0123456789abcdef" for character in subject_hash
        ):
            raise ValueError("Google subject must be represented by a SHA-256 digest.")

        limit_ref = self._client.document(f"identity_attempt_limits/google-install-{subject_hash}")
        installation_ref = self._client.document(f"installations/{device.device_id}")
        cutoff = _utc(now) - window

        def commit(transaction: Any) -> None:
            limit_snapshot = _single_snapshot(transaction, limit_ref)
            limit_document = (
                dict(limit_snapshot.to_dict() or {})
                if limit_snapshot is not None and limit_snapshot.exists
                else {}
            )
            timestamps = [
                parsed
                for value in limit_document.get("created_at", [])
                if (parsed := _parse_timestamp(value)) is not None and parsed > cutoff
            ]
            if len(timestamps) >= max_per_hour:
                raise GoogleIdentityRateLimited()
            timestamps.append(_utc(now))
            installation_snapshot = _single_snapshot(transaction, installation_ref)
            if installation_snapshot is not None and installation_snapshot.exists:
                raise ValueError("Generated Google installation identifier already exists.")

            mapped_device, device_document = _identity_write(device)
            transaction.create(installation_ref, device_document)
            transaction.set(
                limit_ref,
                {
                    "subject_hash": subject_hash,
                    "created_at": [_iso(value) for value in timestamps],
                    "expires_at": max(timestamps) + window,
                },
            )

        await asyncio.to_thread(
            _run_transaction,
            self._client,
            commit,
            max_attempts=20,
        )

    async def bootstrap_install(
        self,
        *,
        challenge_hash: str,
        device: DeviceRecord,
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
    ) -> bool:
        challenge_ref = self._client.document(f"bootstrap_challenges/{challenge_hash}")
        mapped, device_document = _identity_write(device)
        device_ref = self._client.document(mapped.path)
        credentials = self._client.collection("installations").where(
            filter=FieldFilter("credential_hash", "==", device.credential_hash)
        )
        result = {"accepted": False, "error": ""}
        current_time = _utc(now)
        consumed_at = _iso(current_time)

        def commit(transaction: Any) -> None:
            result.update({"accepted": False, "error": ""})
            challenge = _single_snapshot(transaction, challenge_ref)
            challenge_document = (
                dict(challenge.to_dict() or {})
                if challenge is not None and challenge.exists
                else {}
            )
            if (
                challenge is None
                or not challenge.exists
                or challenge_document.get("id") != challenge_hash
                or challenge_document.get("record_type") != "bootstrap_challenge"
            ):
                result["error"] = "invalid"
                return
            if _cooldown_active(challenge_document, current_time):
                result["error"] = "limited"
                return
            expiry = _parse_timestamp(challenge_document.get("expires_at"))
            if (
                challenge_document.get("consumed_at") is not None
                or expiry is None
                or expiry <= current_time
            ):
                fields, limited = _challenge_failure_state(
                    challenge_document,
                    now=current_time,
                    failure_limit=failure_limit,
                    failure_window=failure_window,
                    cooldown=cooldown,
                )
                transaction.update(challenge_ref, fields)
                result["error"] = "limited" if limited else "invalid"
                return
            existing_devices = list(transaction.get(credentials))
            device_snapshot = _single_snapshot(transaction, device_ref)
            if existing_devices or device_snapshot.exists:
                fields, limited = _challenge_failure_state(
                    challenge_document,
                    now=current_time,
                    failure_limit=failure_limit,
                    failure_window=failure_window,
                    cooldown=cooldown,
                )
                transaction.update(challenge_ref, fields)
                result["error"] = "limited" if limited else "invalid"
                return
            transaction.update(
                challenge_ref,
                {"consumed_at": consumed_at, "failed_at": [], "cooldown_until": None},
            )
            transaction.create(device_ref, device_document)
            result["accepted"] = True

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"] == "limited":
            raise ChallengeRateLimited()
        return result["accepted"]

    async def find_active_device_by_credential_hash(
        self, credential_hash: str
    ) -> DeviceRecord | None:
        query = self._client.collection("installations").where(
            filter=FieldFilter("credential_hash", "==", credential_hash)
        )
        snapshots = await asyncio.to_thread(lambda: list(query.stream()))
        if len(snapshots) > 1:
            raise ValueError("Credential hash is not unique in Firestore.")
        if not snapshots:
            return None
        record = _read_model(DeviceRecord, snapshots[0].reference.path, snapshots[0])
        return record if record is not None and record.revoked_at is None else None

    async def create_household(
        self,
        *,
        device_id: str,
        creation: HouseholdCreation,
        now: datetime,
        max_per_hour: int,
        window: timedelta,
    ) -> None:
        household_mapped, household_document = _identity_write(creation.household)
        membership_mapped, membership_document = _membership_write(creation.owner_membership)
        device_ref = self._client.document(f"installations/{device_id}")
        household_ref = self._client.document(household_mapped.path)
        membership_ref = self._client.document(membership_mapped.path)
        limit_ref = self._client.document(f"identity_attempt_limits/household-create-{device_id}")
        current_time = _utc(now)
        cutoff = current_time - window
        result = {"limited": False}

        def commit(transaction: Any) -> None:
            result["limited"] = False
            device_snapshot = _single_snapshot(transaction, device_ref)
            household_snapshot = _single_snapshot(transaction, household_ref)
            membership_snapshot = _single_snapshot(transaction, membership_ref)
            limit_snapshot = _single_snapshot(transaction, limit_ref)
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            if device is None or device.revoked_at is not None:
                raise ForbiddenIdentityAction()
            if household_snapshot.exists or membership_snapshot.exists:
                raise MembershipConflict()
            limit_document = (
                dict(limit_snapshot.to_dict() or {})
                if limit_snapshot is not None and limit_snapshot.exists
                else {}
            )
            timestamps = [
                parsed
                for value in limit_document.get("created_at", [])
                if (parsed := _parse_timestamp(value)) is not None and parsed > cutoff
            ]
            if len(timestamps) >= max_per_hour:
                transaction.set(
                    limit_ref,
                    {
                        "installation_id": device_id,
                        "created_at": [_iso(value) for value in timestamps],
                        "expires_at": max(timestamps) + window,
                    },
                )
                result["limited"] = True
                return
            timestamps.append(current_time)
            transaction.create(household_ref, household_document)
            transaction.create(membership_ref, membership_document)
            if device.default_household_id is None:
                default_device = device.model_copy(
                    update={"default_household_id": creation.household.household_id}
                )
                _, default_device_document = _identity_write(default_device)
                transaction.set(device_ref, default_device_document)
            transaction.set(
                limit_ref,
                {
                    "installation_id": device_id,
                    "created_at": [_iso(value) for value in timestamps],
                    "expires_at": max(timestamps) + window,
                },
            )

        await asyncio.to_thread(
            _run_transaction,
            self._client,
            commit,
            max_attempts=20,
        )
        if result["limited"]:
            raise HouseholdCreationRateLimited()

    async def list_device_memberships(self, device_id: str) -> DeviceMemberships:
        query = self._client.collection("household_memberships").where(
            filter=FieldFilter("installation_id", "==", device_id)
        )
        snapshots = await asyncio.to_thread(lambda: list(query.stream()))
        memberships = tuple(
            membership
            for snapshot in snapshots
            if (membership := _membership_from_snapshot(snapshot)) is not None
        )
        return DeviceMemberships(device_id=device_id, memberships=memberships)

    async def get_active_membership(
        self, *, device_id: str, household_id: str
    ) -> HouseholdMembership | None:
        reference = _membership_reference(self._client, device_id, household_id)
        snapshot = await asyncio.to_thread(reference.get)
        membership = _membership_from_snapshot(snapshot, household_id=household_id)
        if membership is None or not membership.is_active:
            return None
        return membership

    async def create_invite(
        self,
        *,
        actor_device_id: str,
        invite: HouseholdInviteRecord,
        now: datetime,
        max_per_hour: int,
    ) -> None:
        actor_ref = _membership_reference(self._client, actor_device_id, invite.household_id)
        device_ref = self._client.document(f"installations/{actor_device_id}")
        invite_mapped, invite_document = _identity_write(invite)
        invite_ref = self._client.document(invite_mapped.path)
        limit_ref = self._client.document(f"invite_limits/{invite.household_id}")
        result = {"error": ""}
        current_time = _utc(now)
        cutoff = _iso(current_time - timedelta(hours=1))

        def commit(transaction: Any) -> None:
            result["error"] = ""
            actor_snapshot = _single_snapshot(transaction, actor_ref)
            device_snapshot = _single_snapshot(transaction, device_ref)
            limit_snapshot = _single_snapshot(transaction, limit_ref)
            invite_snapshot = _single_snapshot(transaction, invite_ref)
            actor = (
                _membership_from_snapshot(actor_snapshot, household_id=invite.household_id)
                if actor_snapshot is not None and actor_snapshot.exists
                else None
            )
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            if (
                actor is None
                or not actor.is_active
                or actor.role not in (HouseholdRole.OWNER, HouseholdRole.ADMIN)
                or device is None
                or device.revoked_at is not None
            ):
                result["error"] = "forbidden"
                return
            if invite_snapshot.exists:
                result["error"] = "conflict"
                return
            limit_doc = dict(limit_snapshot.to_dict() or {}) if limit_snapshot.exists else {}
            issued = [
                value
                for value in limit_doc.get("issued_at", [])
                if isinstance(value, str) and value >= cutoff
            ]
            if len(issued) >= max_per_hour:
                parsed_issued = [
                    parsed for value in issued if (parsed := _parse_timestamp(value)) is not None
                ]
                if parsed_issued:
                    prior_expiry = _parse_timestamp(limit_doc.get("expires_at"))
                    limit_doc["expires_at"] = max(
                        max(parsed_issued) + timedelta(hours=1),
                        prior_expiry or datetime.min.replace(tzinfo=UTC),
                    )
                    transaction.set(limit_ref, limit_doc)
                result["error"] = "limited"
                return
            issued.append(_iso(current_time))
            parsed_issued = [parsed for value in issued if (parsed := _parse_timestamp(value))]
            transaction.create(invite_ref, invite_document)
            transaction.set(
                limit_ref,
                {
                    "household_id": invite.household_id,
                    "issued_at": issued,
                    "expires_at": max(parsed_issued) + timedelta(hours=1),
                },
            )

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"] == "forbidden":
            raise ForbiddenIdentityAction()
        if result["error"] == "limited":
            raise InviteRateLimited()
        if result["error"]:
            raise MembershipConflict()

    async def redeem_invite(
        self,
        *,
        token_hash: str,
        device_id: str,
        network_key: str,
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
    ) -> HouseholdMembership:
        rate_digest = hashlib.sha256(f"{device_id}\0{network_key}".encode()).hexdigest()
        rate_ref = self._client.document(f"invite_attempts/{rate_digest}")
        invite_query = self._client.collection_group("invites").where(
            filter=FieldFilter("token_hash", "==", token_hash)
        )
        device_ref = self._client.document(f"installations/{device_id}")
        result: dict[str, Any] = {"membership": None, "error": ""}
        current_time = _utc(now)

        def commit(transaction: Any) -> None:
            result.update({"membership": None, "error": ""})
            rate_snapshot = _single_snapshot(transaction, rate_ref)
            rate_document = (
                dict(rate_snapshot.to_dict() or {})
                if rate_snapshot and rate_snapshot.exists
                else {}
            )
            cooldown_until = rate_document.get("cooldown_until")
            if isinstance(cooldown_until, str) and cooldown_until > _iso(current_time):
                transaction.set(
                    rate_ref,
                    {
                        **rate_document,
                        "expires_at": _failure_state_expiry(
                            rate_document,
                            now=current_time,
                            failure_window=failure_window,
                        ),
                    },
                )
                result["error"] = "limited"
                return

            invite_matches = list(transaction.get(invite_query))
            invite_snapshot = invite_matches[0] if len(invite_matches) == 1 else None
            invite_record = (
                _read_model(
                    HouseholdInviteRecord,
                    invite_snapshot.reference.path,
                    invite_snapshot,
                )
                if invite_snapshot is not None
                else None
            )
            device_snapshot = _single_snapshot(transaction, device_ref)
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            membership_ref = (
                _membership_reference(self._client, device_id, invite_record.household_id)
                if invite_record is not None
                else None
            )
            membership_snapshot = (
                _single_snapshot(transaction, membership_ref)
                if membership_ref is not None
                else None
            )
            invite_valid = (
                invite_record is not None
                and invite_record.token_hash == token_hash
                and invite_record.redeemed_at is None
                and invite_record.expires_at > current_time
                and device is not None
                and device.revoked_at is None
                and membership_snapshot is not None
                and not membership_snapshot.exists
            )
            if not invite_valid:
                fields, limited = _invite_failure_state(
                    rate_document,
                    now=current_time,
                    failure_limit=failure_limit,
                    failure_window=failure_window,
                    cooldown=cooldown,
                )
                result["error"] = "limited" if limited else "invalid"
                transaction.set(rate_ref, fields)
                return

            assert invite_record is not None and membership_ref is not None
            redeemed = invite_record.model_copy(
                update={
                    "redeemed_at": current_time,
                    "redeemed_by_device_id": device_id,
                }
            )
            _, invite_document = _identity_write(redeemed)
            membership = HouseholdMembership(
                household_id=invite_record.household_id,
                device_id=device_id,
                role=invite_record.granted_role,
                created_at=current_time,
            )
            _, membership_document = _membership_write(membership)
            transaction.set(invite_snapshot.reference, invite_document)
            transaction.create(membership_ref, membership_document)
            if rate_snapshot is not None and rate_snapshot.exists:
                transaction.delete(rate_ref)
            result["membership"] = membership

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"] == "limited":
            raise InviteRateLimited()
        if result["error"]:
            raise InviteRedemptionRejected()
        membership = result["membership"]
        if not isinstance(membership, HouseholdMembership):
            raise InviteRedemptionRejected()
        return membership

    async def leave_household(self, *, device_id: str, household_id: str, now: datetime) -> None:
        membership_ref = _membership_reference(self._client, device_id, household_id)
        device_ref = self._client.document(f"installations/{device_id}")
        result = {"error": ""}

        def commit(transaction: Any) -> None:
            result["error"] = ""
            membership_snapshot = _single_snapshot(transaction, membership_ref)
            device_snapshot = _single_snapshot(transaction, device_ref)
            membership = (
                _membership_from_snapshot(membership_snapshot, household_id=household_id)
                if membership_snapshot is not None and membership_snapshot.exists
                else None
            )
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            if (
                membership is None
                or not membership.is_active
                or device is None
                or device.revoked_at
            ):
                result["error"] = "forbidden"
                return
            if membership.role is HouseholdRole.OWNER:
                result["error"] = "owner"
                return
            _, document = _membership_write(membership.model_copy(update={"revoked_at": now}))
            transaction.set(membership_ref, document)

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"] == "owner":
            raise MembershipConflict("Transfer ownership before leaving.")
        if result["error"]:
            raise ForbiddenIdentityAction()

    async def set_membership_role(
        self,
        *,
        actor_device_id: str,
        household_id: str,
        target_device_id: str,
        role: HouseholdRole,
        now: datetime,
    ) -> HouseholdMembership:
        actor_ref = _membership_reference(self._client, actor_device_id, household_id)
        target_ref = _membership_reference(self._client, target_device_id, household_id)
        actor_device_ref = self._client.document(f"installations/{actor_device_id}")
        target_device_ref = self._client.document(f"installations/{target_device_id}")
        owner_query = (
            self._client.collection("household_memberships")
            .where(filter=FieldFilter("household_id", "==", household_id))
            .where(filter=FieldFilter("role", "==", HouseholdRole.OWNER.value))
            .where(filter=FieldFilter("revoked_at", "==", None))
        )
        result: dict[str, Any] = {"membership": None, "error": ""}

        def commit(transaction: Any) -> None:
            result.update({"membership": None, "error": ""})
            actor_snapshot = _single_snapshot(transaction, actor_ref)
            target_snapshot = (
                actor_snapshot
                if actor_device_id == target_device_id
                else _single_snapshot(transaction, target_ref)
            )
            actor_device_snapshot = _single_snapshot(transaction, actor_device_ref)
            target_device_snapshot = (
                actor_device_snapshot
                if actor_device_id == target_device_id
                else _single_snapshot(transaction, target_device_ref)
            )
            actor = (
                _membership_from_snapshot(actor_snapshot, household_id=household_id)
                if actor_snapshot is not None and actor_snapshot.exists
                else None
            )
            target = (
                _membership_from_snapshot(target_snapshot, household_id=household_id)
                if target_snapshot is not None and target_snapshot.exists
                else None
            )
            actor_device = (
                _read_model(DeviceRecord, actor_device_ref.path, actor_device_snapshot)
                if actor_device_snapshot is not None and actor_device_snapshot.exists
                else None
            )
            target_device = (
                _read_model(DeviceRecord, target_device_ref.path, target_device_snapshot)
                if target_device_snapshot is not None and target_device_snapshot.exists
                else None
            )
            owners = list(transaction.get(owner_query)) if role is HouseholdRole.OWNER else []
            if (
                actor is None
                or not actor.is_active
                or target is None
                or not target.is_active
                or actor_device is None
                or actor_device.revoked_at is not None
                or target_device is None
                or target_device.revoked_at is not None
            ):
                result["error"] = "forbidden"
                return
            if actor.role is HouseholdRole.OWNER:
                if target.role is HouseholdRole.OWNER:
                    result["error"] = "conflict"
                    return
                if role is HouseholdRole.OWNER:
                    if actor_device_id == target_device_id or len(owners) != 1:
                        result["error"] = "conflict"
                        return
                    actor_updated = actor.model_copy(update={"role": HouseholdRole.ADMIN})
                    _, actor_document = _membership_write(actor_updated)
                    transaction.set(actor_ref, actor_document)
            elif actor.role is HouseholdRole.ADMIN:
                if target.role is not HouseholdRole.MEMBER or role is not HouseholdRole.MEMBER:
                    result["error"] = "forbidden"
                    return
            else:
                result["error"] = "forbidden"
                return
            updated = target.model_copy(update={"role": role})
            _, target_document = _membership_write(updated)
            transaction.set(target_ref, target_document)
            result["membership"] = updated

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"] == "conflict":
            raise MembershipConflict()
        if result["error"]:
            raise ForbiddenIdentityAction()
        membership = result["membership"]
        if not isinstance(membership, HouseholdMembership):
            raise ForbiddenIdentityAction()
        return membership

    async def revoke_membership(
        self,
        *,
        actor_device_id: str,
        household_id: str,
        target_device_id: str,
        now: datetime,
    ) -> None:
        actor_ref = _membership_reference(self._client, actor_device_id, household_id)
        target_ref = _membership_reference(self._client, target_device_id, household_id)
        actor_device_ref = self._client.document(f"installations/{actor_device_id}")
        target_device_ref = self._client.document(f"installations/{target_device_id}")
        result = {"error": ""}

        def commit(transaction: Any) -> None:
            result["error"] = ""
            actor_snapshot = _single_snapshot(transaction, actor_ref)
            target_snapshot = (
                actor_snapshot
                if actor_device_id == target_device_id
                else _single_snapshot(transaction, target_ref)
            )
            actor_device_snapshot = _single_snapshot(transaction, actor_device_ref)
            target_device_snapshot = (
                actor_device_snapshot
                if actor_device_id == target_device_id
                else _single_snapshot(transaction, target_device_ref)
            )
            actor = (
                _membership_from_snapshot(actor_snapshot, household_id=household_id)
                if actor_snapshot is not None and actor_snapshot.exists
                else None
            )
            target = (
                _membership_from_snapshot(target_snapshot, household_id=household_id)
                if target_snapshot is not None and target_snapshot.exists
                else None
            )
            actor_device = (
                _read_model(DeviceRecord, actor_device_ref.path, actor_device_snapshot)
                if actor_device_snapshot is not None and actor_device_snapshot.exists
                else None
            )
            target_device = (
                _read_model(DeviceRecord, target_device_ref.path, target_device_snapshot)
                if target_device_snapshot is not None and target_device_snapshot.exists
                else None
            )
            if (
                actor is None
                or not actor.is_active
                or target is None
                or not target.is_active
                or actor_device is None
                or actor_device.revoked_at is not None
                or target_device is None
                or target_device.revoked_at is not None
                or actor_device_id == target_device_id
            ):
                result["error"] = "forbidden"
                return
            allowed = actor.role is HouseholdRole.OWNER and target.role is not HouseholdRole.OWNER
            allowed = allowed or (
                actor.role is HouseholdRole.ADMIN and target.role is HouseholdRole.MEMBER
            )
            if not allowed:
                result["error"] = "forbidden"
                return
            _, document = _membership_write(target.model_copy(update={"revoked_at": now}))
            transaction.set(target_ref, document)

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"]:
            raise ForbiddenIdentityAction()

    async def issue_device_recovery(
        self, *, actor_device_id: str, recovery: DeviceRecoveryRecord
    ) -> None:
        device_ref = self._client.document(f"installations/{actor_device_id}")
        mapped, document = _identity_write(recovery)
        recovery_ref = self._client.document(mapped.path)
        old_query = self._client.collection(f"installations/{actor_device_id}/recoveries").where(
            filter=FieldFilter("consumed_at", "==", None)
        )
        result = {"allowed": False}

        def commit(transaction: Any) -> None:
            result["allowed"] = False
            device_snapshot = _single_snapshot(transaction, device_ref)
            old_recoveries = list(transaction.get(old_query))
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            if device is None or device.revoked_at is not None:
                return
            if _single_snapshot(transaction, recovery_ref).exists:
                return
            for snapshot in old_recoveries:
                transaction.delete(snapshot.reference)
            transaction.create(recovery_ref, document)
            result["allowed"] = True

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if not result["allowed"]:
            raise ForbiddenIdentityAction()

    async def redeem_device_recovery(
        self,
        *,
        code_hash: str,
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
    ) -> tuple[str, int]:
        recovery_query = self._client.collection_group("recoveries").where(
            filter=FieldFilter("code_hash", "==", code_hash)
        )
        result: dict[str, Any] = {"value": None, "error": ""}
        current_time = _utc(now)

        def commit(transaction: Any) -> None:
            result.update({"value": None, "error": ""})
            matches = list(transaction.get(recovery_query))
            if len(matches) != 1:
                result["error"] = "invalid"
                return
            recovery_snapshot = matches[0]
            document = recovery_snapshot.to_dict() or {}
            if document.get("record_type") != "installation_recovery":
                result["error"] = "invalid"
                return
            if _cooldown_active(document, current_time):
                result["error"] = "limited"
                return
            recovery = _read_model(
                DeviceRecoveryRecord,
                recovery_snapshot.reference.path,
                recovery_snapshot,
            )
            if recovery is None or recovery.consumed_at is not None:
                self._record_challenge_failure(
                    transaction,
                    recovery_ref=recovery_snapshot.reference,
                    recovery_document=document,
                    now=current_time,
                    failure_limit=failure_limit,
                    failure_window=failure_window,
                    cooldown=cooldown,
                    result=result,
                )
                return
            device_ref = self._client.document(f"installations/{recovery.device_id}")
            device_snapshot = _single_snapshot(transaction, device_ref)
            memberships_query = self._client.collection("household_memberships").where(
                filter=FieldFilter("installation_id", "==", recovery.device_id)
            )
            membership_snapshots = list(transaction.get(memberships_query))
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            if device is None or device.revoked_at is not None:
                self._record_challenge_failure(
                    transaction,
                    recovery_ref=recovery_snapshot.reference,
                    recovery_document=document,
                    now=current_time,
                    failure_limit=failure_limit,
                    failure_window=failure_window,
                    cooldown=cooldown,
                    result=result,
                )
                return
            active_memberships = []
            for snapshot in membership_snapshots:
                membership = _membership_from_snapshot(snapshot)
                if membership is not None and membership.is_active:
                    active_memberships.append((snapshot.reference, membership))
            revoked_device = device.model_copy(update={"revoked_at": now})
            _, device_document = _identity_write(revoked_device)
            consumed = recovery.model_copy(update={"consumed_at": now})
            _, recovery_document = _identity_write(consumed)
            transaction.set(device_ref, device_document)
            transaction.set(recovery_snapshot.reference, recovery_document)
            for reference, membership in active_memberships:
                _, membership_document = _membership_write(
                    membership.model_copy(update={"revoked_at": now})
                )
                transaction.set(reference, membership_document)
            result["value"] = (recovery.device_id, len(active_memberships))

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"] == "limited":
            raise ChallengeRateLimited()
        if result["value"] is None:
            raise InviteRedemptionRejected()
        return result["value"]

    @staticmethod
    def _record_challenge_failure(
        transaction: Any,
        *,
        recovery_ref: Any,
        recovery_document: dict[str, Any],
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
        result: dict[str, Any],
    ) -> None:
        fields, limited = _challenge_failure_state(
            recovery_document,
            now=now,
            failure_limit=failure_limit,
            failure_window=failure_window,
            cooldown=cooldown,
        )
        transaction.update(recovery_ref, fields)
        result["error"] = "limited" if limited else "invalid"

    async def issue_household_recovery(
        self, *, actor_device_id: str, recovery: HouseholdRecoveryRecord
    ) -> None:
        actor_ref = _membership_reference(self._client, actor_device_id, recovery.household_id)
        device_ref = self._client.document(f"installations/{actor_device_id}")
        mapped, document = _identity_write(recovery)
        recovery_ref = self._client.document(mapped.path)
        old_query = self._client.collection(f"households/{recovery.household_id}/recoveries").where(
            filter=FieldFilter("consumed_at", "==", None)
        )
        result = {"allowed": False}

        def commit(transaction: Any) -> None:
            result["allowed"] = False
            actor_snapshot = _single_snapshot(transaction, actor_ref)
            device_snapshot = _single_snapshot(transaction, device_ref)
            old_recoveries = list(transaction.get(old_query))
            actor = (
                _membership_from_snapshot(actor_snapshot, household_id=recovery.household_id)
                if actor_snapshot is not None and actor_snapshot.exists
                else None
            )
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            if (
                actor is None
                or not actor.is_active
                or actor.role is not HouseholdRole.OWNER
                or device is None
                or device.revoked_at is not None
                or _single_snapshot(transaction, recovery_ref).exists
            ):
                return
            for snapshot in old_recoveries:
                transaction.delete(snapshot.reference)
            transaction.create(recovery_ref, document)
            result["allowed"] = True

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if not result["allowed"]:
            raise ForbiddenIdentityAction()

    async def redeem_household_recovery(
        self,
        *,
        code_hash: str,
        device_id: str,
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
    ) -> HouseholdMembership:
        recovery_query = self._client.collection_group("recoveries").where(
            filter=FieldFilter("code_hash", "==", code_hash)
        )
        device_ref = self._client.document(f"installations/{device_id}")
        result: dict[str, Any] = {"membership": None, "error": ""}
        current_time = _utc(now)

        def commit(transaction: Any) -> None:
            result.update({"membership": None, "error": ""})
            matches = list(transaction.get(recovery_query))
            if len(matches) != 1:
                result["error"] = "invalid"
                return
            recovery_snapshot = matches[0]
            recovery_document = recovery_snapshot.to_dict() or {}
            if recovery_document.get("record_type") != "household_recovery":
                result["error"] = "invalid"
                return
            if _cooldown_active(recovery_document, current_time):
                result["error"] = "limited"
                return
            recovery = _read_model(
                HouseholdRecoveryRecord,
                recovery_snapshot.reference.path,
                recovery_snapshot,
            )
            if recovery is None or recovery.consumed_at is not None:
                self._record_challenge_failure(
                    transaction,
                    recovery_ref=recovery_snapshot.reference,
                    recovery_document=recovery_document,
                    now=current_time,
                    failure_limit=failure_limit,
                    failure_window=failure_window,
                    cooldown=cooldown,
                    result=result,
                )
                return
            device_snapshot = _single_snapshot(transaction, device_ref)
            household_ref = self._client.document(f"households/{recovery.household_id}")
            household_snapshot = _single_snapshot(transaction, household_ref)
            membership_ref = _membership_reference(self._client, device_id, recovery.household_id)
            membership_snapshot = _single_snapshot(transaction, membership_ref)
            owner_query = (
                self._client.collection("household_memberships")
                .where(filter=FieldFilter("household_id", "==", recovery.household_id))
                .where(filter=FieldFilter("role", "==", HouseholdRole.OWNER.value))
                .where(filter=FieldFilter("revoked_at", "==", None))
            )
            owners = list(transaction.get(owner_query))
            device = (
                _read_model(DeviceRecord, device_ref.path, device_snapshot)
                if device_snapshot is not None and device_snapshot.exists
                else None
            )
            if (
                device is None
                or device.revoked_at is not None
                or household_snapshot is None
                or not household_snapshot.exists
                or any(
                    (owner.to_dict() or {}).get("installation_id") != device_id for owner in owners
                )
            ):
                self._record_challenge_failure(
                    transaction,
                    recovery_ref=recovery_snapshot.reference,
                    recovery_document=recovery_document,
                    now=current_time,
                    failure_limit=failure_limit,
                    failure_window=failure_window,
                    cooldown=cooldown,
                    result=result,
                )
                return
            membership = HouseholdMembership(
                household_id=recovery.household_id,
                device_id=device_id,
                role=HouseholdRole.OWNER,
                created_at=now,
                revoked_at=(
                    None if membership_snapshot is None or not membership_snapshot.exists else None
                ),
            )
            _, membership_document = _membership_write(membership)
            consumed = recovery.model_copy(update={"consumed_at": now})
            _, recovery_document = _identity_write(consumed)
            if membership_snapshot is None or not membership_snapshot.exists:
                transaction.create(membership_ref, membership_document)
            else:
                transaction.set(membership_ref, membership_document)
            transaction.set(recovery_snapshot.reference, recovery_document)
            result["membership"] = membership

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["error"] == "limited":
            raise ChallengeRateLimited()
        membership = result["membership"]
        if not isinstance(membership, HouseholdMembership):
            raise InviteRedemptionRejected()
        return membership
