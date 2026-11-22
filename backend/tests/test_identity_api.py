from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.agent.read_agent import get_read_agent
from app.core.dependencies import (
    get_approval_service,
    get_identity_service,
    get_inventory_service,
    get_media_storage_service,
    get_required_google_identity_service,
)
from app.main import create_app
from app.models.identity import (
    DeviceMemberships,
    DeviceRecord,
    DeviceRecoveryRecord,
    HouseholdCreation,
    HouseholdInviteRecord,
    HouseholdMembership,
    HouseholdRecord,
    HouseholdRecoveryRecord,
    HouseholdRole,
)
from app.services.identity import (
    ChallengeRateLimited,
    ForbiddenIdentityAction,
    HouseholdCreationRateLimited,
    IdentityService,
    InvalidBootstrapChallenge,
    InvalidCredential,
    InvalidInvite,
    InviteRateLimited,
    InviteRedemptionRejected,
    MembershipConflict,
    RecoveryRejected,
    hash_secret,
)
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage

NOW = datetime(2026, 11, 19, 5, 0, tzinfo=UTC)
BOOTSTRAP_1 = "install-challenge-0001"
BOOTSTRAP_2 = "install-challenge-0002"
BOOTSTRAP_3 = "install-challenge-0003"


def test_google_identity_exchange_returns_default_household_response():
    class GoogleService:
        async def authenticate(self, token: str) -> tuple[str, str, str]:
            assert token == "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.c2ln"
            return "install-1", "secure-bearer-credential-value", "household-1"

    app = create_app()
    app.dependency_overrides[get_required_google_identity_service] = GoogleService
    with TestClient(app) as client:
        response = client.post(
            "/identity/google",
            json={"google_id_token": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.c2ln"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "device_id": "install-1",
        "household_id": "household-1",
        "bearer_credential": "secure-bearer-credential-value",
    }


def test_invite_rate_limit_source_ignores_client_forwarding_headers():
    from app.api.identity import _invite_rate_limit_source

    def request(peer: str, forwarded: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "path": "/identity/bootstrap",
                "query_string": b"",
                "headers": [
                    (b"x-forwarded-for", forwarded.encode()),
                    (b"x-forwarded-host", b"attacker.example"),
                    (b"forwarded", b"for=198.51.100.77;proto=http;host=attacker.example"),
                ],
                "client": (peer, 443),
                "server": ("nestock-stg-api.example", 443),
                "http_version": "1.1",
            }
        )

    assert _invite_rate_limit_source(request("192.0.2.10", "203.0.113.1")) == (
        "unverified-invite-ingress"
    )
    assert _invite_rate_limit_source(request("192.0.2.11", "203.0.113.2")) == (
        "unverified-invite-ingress"
    )


class FakeIdentityRepository:
    """Atomic in-process test fake; never used by runtime composition."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.bootstrap_challenges = {
            hash_secret(BOOTSTRAP_1),
            hash_secret(BOOTSTRAP_2),
            hash_secret(BOOTSTRAP_3),
        }
        self.known_bootstrap_challenges = set(self.bootstrap_challenges)
        self.devices: dict[str, DeviceRecord] = {}
        self.households: dict[str, HouseholdRecord] = {}
        self.memberships: dict[tuple[str, str], HouseholdMembership] = {}
        self.invites: dict[str, HouseholdInviteRecord] = {}
        self.device_recoveries: dict[str, DeviceRecoveryRecord] = {}
        self.household_recoveries: dict[str, HouseholdRecoveryRecord] = {}
        self.invite_created: dict[str, list[datetime]] = defaultdict(list)
        self.invite_failures: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        self.invite_cooldowns: dict[tuple[str, str], datetime] = {}
        self.challenge_failures: dict[str, list[datetime]] = defaultdict(list)
        self.challenge_cooldowns: dict[str, datetime] = {}
        self.household_created: dict[str, list[datetime]] = defaultdict(list)

    def _record_challenge_failure(
        self,
        record_key,
        now,
        failure_limit,
        failure_window,
        cooldown,
    ):
        if self.challenge_cooldowns.get(record_key, datetime.min.replace(tzinfo=UTC)) > now:
            raise ChallengeRateLimited()
        cutoff = now - failure_window
        failures = [item for item in self.challenge_failures[record_key] if item > cutoff]
        failures.append(now)
        self.challenge_failures[record_key] = failures
        if len(failures) >= failure_limit:
            self.challenge_cooldowns[record_key] = now + cooldown
            raise ChallengeRateLimited()

    async def bootstrap_install(
        self,
        *,
        challenge_hash,
        device,
        now,
        failure_limit,
        failure_window,
        cooldown,
    ):
        with self.lock:
            if challenge_hash not in self.known_bootstrap_challenges:
                return False
            if self.challenge_cooldowns.get(challenge_hash, datetime.min.replace(tzinfo=UTC)) > now:
                raise ChallengeRateLimited()
            if challenge_hash not in self.bootstrap_challenges:
                self._record_challenge_failure(
                    challenge_hash,
                    now,
                    failure_limit,
                    failure_window,
                    cooldown,
                )
                return False
            self.bootstrap_challenges.remove(challenge_hash)
            self.devices[device.device_id] = device
            self.challenge_failures.pop(challenge_hash, None)
            self.challenge_cooldowns.pop(challenge_hash, None)
            return True

    async def find_active_device_by_credential_hash(self, credential_hash: str):
        with self.lock:
            return next(
                (
                    device
                    for device in self.devices.values()
                    if device.credential_hash == credential_hash and device.revoked_at is None
                ),
                None,
            )

    def _active_device(self, device_id: str) -> bool:
        device = self.devices.get(device_id)
        return device is not None and device.revoked_at is None

    async def create_household(
        self, *, device_id: str, creation: HouseholdCreation, now, max_per_hour, window
    ) -> None:
        with self.lock:
            if not self._active_device(device_id):
                raise ForbiddenIdentityAction()
            hid = creation.household.household_id
            if hid in self.households:
                raise MembershipConflict()
            cutoff = now - window
            recent = [item for item in self.household_created[device_id] if item > cutoff]
            self.household_created[device_id] = recent
            if len(recent) >= max_per_hour:
                raise HouseholdCreationRateLimited()
            self.households[hid] = creation.household
            membership = creation.owner_membership
            self.memberships[(device_id, hid)] = membership
            self.household_created[device_id].append(now)

    async def list_device_memberships(self, device_id: str) -> DeviceMemberships:
        with self.lock:
            if not self._active_device(device_id):
                raise ForbiddenIdentityAction()
            return DeviceMemberships(
                device_id=device_id,
                memberships=tuple(
                    membership
                    for (member_device_id, _), membership in self.memberships.items()
                    if member_device_id == device_id
                ),
            )

    async def get_active_membership(self, *, device_id: str, household_id: str):
        with self.lock:
            if not self._active_device(device_id):
                return None
            membership = self.memberships.get((device_id, household_id))
            return membership if membership and membership.is_active else None

    async def create_invite(self, *, actor_device_id, invite, now, max_per_hour) -> None:
        with self.lock:
            membership = self.memberships.get((actor_device_id, invite.household_id))
            if (
                not self._active_device(actor_device_id)
                or membership is None
                or not membership.is_active
                or membership.role not in (HouseholdRole.OWNER, HouseholdRole.ADMIN)
            ):
                raise ForbiddenIdentityAction()
            cutoff = now - timedelta(hours=1)
            active_times = [
                item for item in self.invite_created[invite.household_id] if item > cutoff
            ]
            self.invite_created[invite.household_id] = active_times
            if len(active_times) >= max_per_hour:
                raise InviteRateLimited()
            self.invite_created[invite.household_id].append(now)
            self.invites[invite.token_hash] = invite

    async def redeem_invite(
        self,
        *,
        token_hash,
        device_id,
        network_key,
        now,
        failure_limit,
        failure_window,
        cooldown,
    ):
        with self.lock:
            if not self._active_device(device_id):
                raise ForbiddenIdentityAction()
            rate_key = (device_id, network_key)
            if self.invite_cooldowns.get(rate_key, datetime.min.replace(tzinfo=UTC)) > now:
                raise InviteRateLimited()
            cutoff = now - failure_window
            failures = [item for item in self.invite_failures[rate_key] if item > cutoff]
            self.invite_failures[rate_key] = failures
            invite = self.invites.get(token_hash)
            invalid = (
                invite is None
                or invite.redeemed_at is not None
                or invite.expires_at <= now
                or (device_id, invite.household_id) in self.memberships
                and self.memberships[(device_id, invite.household_id)].is_active
            )
            if invalid:
                failures.append(now)
                self.invite_failures[rate_key] = failures
                if len(failures) >= failure_limit:
                    self.invite_cooldowns[rate_key] = now + cooldown
                raise InviteRedemptionRejected()
            membership = HouseholdMembership(
                household_id=invite.household_id,
                device_id=device_id,
                role=HouseholdRole.MEMBER,
                created_at=now,
            )
            self.memberships[(device_id, invite.household_id)] = membership
            self.invites[token_hash] = invite.model_copy(
                update={"redeemed_at": now, "redeemed_by_device_id": device_id}
            )
            self.invite_failures.pop(rate_key, None)
            self.invite_cooldowns.pop(rate_key, None)
            return membership

    async def leave_household(self, *, device_id, household_id, now):
        with self.lock:
            membership = self.memberships.get((device_id, household_id))
            if not self._active_device(device_id) or membership is None or not membership.is_active:
                raise ForbiddenIdentityAction()
            if membership.role is HouseholdRole.OWNER:
                raise MembershipConflict("Transfer ownership before leaving.")
            self.memberships[(device_id, household_id)] = membership.model_copy(
                update={"revoked_at": now}
            )

    async def set_membership_role(
        self, *, actor_device_id, household_id, target_device_id, role, now
    ):
        with self.lock:
            actor = self.memberships.get((actor_device_id, household_id))
            target = self.memberships.get((target_device_id, household_id))
            if (
                not self._active_device(actor_device_id)
                or actor is None
                or not actor.is_active
                or target is None
                or not target.is_active
            ):
                raise ForbiddenIdentityAction()
            if actor.role is HouseholdRole.OWNER:
                if target.role is HouseholdRole.OWNER and target_device_id != actor_device_id:
                    raise MembershipConflict()
                if role is HouseholdRole.OWNER:
                    if target_device_id == actor_device_id:
                        raise MembershipConflict()
                    self.memberships[(actor_device_id, household_id)] = actor.model_copy(
                        update={"role": HouseholdRole.ADMIN}
                    )
                elif target_device_id == actor_device_id:
                    raise MembershipConflict("Transfer ownership before leaving owner role.")
            elif actor.role is HouseholdRole.ADMIN:
                if target.role is not HouseholdRole.MEMBER or role is not HouseholdRole.MEMBER:
                    raise ForbiddenIdentityAction()
            else:
                raise ForbiddenIdentityAction()
            updated = target.model_copy(update={"role": role})
            self.memberships[(target_device_id, household_id)] = updated
            return updated

    async def revoke_membership(self, *, actor_device_id, household_id, target_device_id, now):
        with self.lock:
            actor = self.memberships.get((actor_device_id, household_id))
            target = self.memberships.get((target_device_id, household_id))
            if (
                not self._active_device(actor_device_id)
                or actor is None
                or not actor.is_active
                or target is None
                or not target.is_active
            ):
                raise ForbiddenIdentityAction()
            allowed = actor.role is HouseholdRole.OWNER and target.role is not HouseholdRole.OWNER
            allowed = allowed or (
                actor.role is HouseholdRole.ADMIN and target.role is HouseholdRole.MEMBER
            )
            if not allowed or actor_device_id == target_device_id:
                raise ForbiddenIdentityAction()
            self.memberships[(target_device_id, household_id)] = target.model_copy(
                update={"revoked_at": now}
            )

    async def issue_device_recovery(self, *, actor_device_id, recovery):
        with self.lock:
            if not self._active_device(actor_device_id):
                raise ForbiddenIdentityAction()
            self.device_recoveries = {
                key: record
                for key, record in self.device_recoveries.items()
                if record.device_id != actor_device_id or record.consumed_at is not None
            }
            self.device_recoveries[recovery.code_hash] = recovery

    async def redeem_device_recovery(
        self,
        *,
        code_hash,
        now,
        failure_limit,
        failure_window,
        cooldown,
    ):
        with self.lock:
            if code_hash not in self.device_recoveries:
                raise InviteRedemptionRejected()
            if self.challenge_cooldowns.get(code_hash, datetime.min.replace(tzinfo=UTC)) > now:
                raise ChallengeRateLimited()
            recovery = self.device_recoveries.get(code_hash)
            if recovery is None or recovery.consumed_at is not None:
                self._record_challenge_failure(
                    code_hash,
                    now,
                    failure_limit,
                    failure_window,
                    cooldown,
                )
                raise InviteRedemptionRejected()
            device = self.devices.get(recovery.device_id)
            if device is None or device.revoked_at is not None:
                self._record_challenge_failure(
                    code_hash,
                    now,
                    failure_limit,
                    failure_window,
                    cooldown,
                )
                raise InviteRedemptionRejected()
            count = 0
            for key, membership in list(self.memberships.items()):
                if membership.device_id == recovery.device_id and membership.is_active:
                    self.memberships[key] = membership.model_copy(update={"revoked_at": now})
                    count += 1
            self.devices[recovery.device_id] = device.model_copy(update={"revoked_at": now})
            self.device_recoveries[code_hash] = recovery.model_copy(update={"consumed_at": now})
            self.challenge_failures.pop(code_hash, None)
            self.challenge_cooldowns.pop(code_hash, None)
            return recovery.device_id, count

    async def issue_household_recovery(self, *, actor_device_id, recovery):
        with self.lock:
            membership = self.memberships.get((actor_device_id, recovery.household_id))
            if (
                not self._active_device(actor_device_id)
                or membership is None
                or not membership.is_active
                or membership.role is not HouseholdRole.OWNER
            ):
                raise ForbiddenIdentityAction()
            self.household_recoveries = {
                key: record
                for key, record in self.household_recoveries.items()
                if record.household_id != recovery.household_id or record.consumed_at is not None
            }
            self.household_recoveries[recovery.code_hash] = recovery

    async def redeem_household_recovery(
        self,
        *,
        code_hash,
        device_id,
        now,
        failure_limit,
        failure_window,
        cooldown,
    ):
        with self.lock:
            if code_hash not in self.household_recoveries:
                raise InviteRedemptionRejected()
            if self.challenge_cooldowns.get(code_hash, datetime.min.replace(tzinfo=UTC)) > now:
                raise ChallengeRateLimited()
            recovery = self.household_recoveries.get(code_hash)
            if (
                recovery is None
                or recovery.consumed_at is not None
                or not self._active_device(device_id)
                or recovery.household_id not in self.households
            ):
                self._record_challenge_failure(
                    code_hash,
                    now,
                    failure_limit,
                    failure_window,
                    cooldown,
                )
                raise InviteRedemptionRejected()
            owners = [
                membership
                for (member_device_id, hid), membership in self.memberships.items()
                if hid == recovery.household_id
                and member_device_id != device_id
                and membership.is_active
                and membership.role is HouseholdRole.OWNER
            ]
            if owners:
                self._record_challenge_failure(
                    code_hash,
                    now,
                    failure_limit,
                    failure_window,
                    cooldown,
                )
                raise InviteRedemptionRejected()
            membership = HouseholdMembership(
                household_id=recovery.household_id,
                device_id=device_id,
                role=HouseholdRole.OWNER,
                created_at=now,
            )
            self.memberships[(device_id, recovery.household_id)] = membership
            self.household_recoveries[code_hash] = recovery.model_copy(update={"consumed_at": now})
            self.challenge_failures.pop(code_hash, None)
            self.challenge_cooldowns.pop(code_hash, None)
            return membership


def make_service(now: list[datetime] | None = None):
    clock = now or [NOW]
    repository = FakeIdentityRepository()
    service = IdentityService(repository, clock=lambda: clock[0])
    return repository, service, clock


def install(repository, service, challenge=BOOTSTRAP_1):
    repository.bootstrap_challenges.add(hash_secret(challenge))
    repository.known_bootstrap_challenges.add(hash_secret(challenge))
    return asyncio.run(service.bootstrap_install(challenge))


def create_household(service, device_id):
    return asyncio.run(service.create_household(device_id))


def create_invite(service, device_id, household_id):
    return asyncio.run(service.create_invite(actor_device_id=device_id, household_id=household_id))


def redeem(service, device_id, code, source="198.51.100.10"):
    return asyncio.run(
        service.redeem_invite(
            device_id=device_id,
            invite_code=code,
            network_source=source,
        )
    )


def test_install_credentials_are_hashed_and_bootstrap_challenge_is_single_use():
    repository, service, _ = make_service()
    device_id, credential = install(repository, service)
    assert credential not in {record.credential_hash for record in repository.devices.values()}
    assert repository.devices[device_id].credential_hash == hash_secret(credential)
    assert asyncio.run(service.authenticate(credential)).device_id == device_id
    with pytest.raises(InvalidBootstrapChallenge):
        asyncio.run(service.bootstrap_install(BOOTSTRAP_1))


def test_join_adds_membership_and_context_authorizes_only_joined_households():
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    first = create_household(service, owner_id)
    second = create_household(service, owner_id)
    member_id, _ = install(repository, service, BOOTSTRAP_2)
    _, first_code, _ = create_invite(service, owner_id, first)
    _, second_code, _ = create_invite(service, owner_id, second)
    redeem(service, member_id, first_code)
    redeem(service, member_id, second_code)

    memberships = asyncio.run(service.list_households(member_id))
    assert {item.household_id for item in memberships} == {first, second}
    for household_id in (first, second):
        assert (
            asyncio.run(
                service.authorize_household(device_id=member_id, household_id=household_id)
            ).household_id
            == household_id
        )
    with pytest.raises(ForbiddenIdentityAction):
        asyncio.run(service.authorize_household(device_id=member_id, household_id="not-joined"))


def test_household_creation_is_limited_per_install_and_recovers_after_window():
    repository, service, clock = make_service()
    owner_id, owner_credential = install(repository, service)
    other_id, _ = install(repository, service, BOOTSTRAP_2)

    created = [create_household(service, owner_id) for _ in range(3)]
    with pytest.raises(HouseholdCreationRateLimited):
        create_household(service, owner_id)
    assert len(set(created)) == 3

    app = create_app()
    app.dependency_overrides[get_identity_service] = lambda: service
    with TestClient(app) as client:
        limited = client.post(
            "/households",
            headers={"Authorization": f"Bearer {owner_credential}"},
            json={},
        )
    assert limited.status_code == 429

    # The limit is per install, and a rolling one-hour window does not ban
    # authorized multi-household creation permanently.
    assert create_household(service, other_id)
    clock[0] = NOW + timedelta(hours=1, microseconds=1)
    assert create_household(service, owner_id)


def test_invites_are_member_only_expiring_single_use_and_rate_limited():
    repository, service, clock = make_service()
    owner_id, _ = install(repository, service)
    household_id = create_household(service, owner_id)
    _, code, expires_at = create_invite(service, owner_id, household_id)
    invite = next(iter(repository.invites.values()))
    assert invite.token_hash == hash_secret(code)
    assert code not in repr(invite)
    assert invite.expires_at - invite.created_at == timedelta(minutes=10)

    member_id, _ = install(repository, service, BOOTSTRAP_2)
    assert redeem(service, member_id, code).role is HouseholdRole.MEMBER
    with pytest.raises(InvalidInvite):
        redeem(service, member_id, code)

    other_id, _ = install(repository, service, BOOTSTRAP_3)
    for _ in range(5):
        with pytest.raises(InvalidInvite):
            redeem(service, other_id, "x" * 40)
    with pytest.raises(InviteRateLimited):
        redeem(service, other_id, "x" * 40)

    late_id, _ = install(repository, service, "install-challenge-0004")
    _, expired_code, expiry = create_invite(service, owner_id, household_id)
    clock[0] = expiry + timedelta(microseconds=1)
    with pytest.raises(InvalidInvite):
        redeem(service, late_id, expired_code, source="198.51.100.11")


def test_invite_creation_is_capped_per_household():
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    household_id = create_household(service, owner_id)
    for _ in range(10):
        create_invite(service, owner_id, household_id)
    with pytest.raises(InviteRateLimited):
        create_invite(service, owner_id, household_id)


def test_identity_audit_logs_never_include_invite_or_recovery_secrets(caplog):
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    household_id = create_household(service, owner_id)
    member_id, _ = install(repository, service, BOOTSTRAP_2)
    with caplog.at_level(logging.INFO, logger="app.services.identity"):
        _, invite_code, _ = create_invite(service, owner_id, household_id)
        redeem(service, member_id, invite_code)
        recovery_code = asyncio.run(service.issue_device_recovery(member_id))
    assert "identity.invite.created" in caplog.text
    assert "identity.invite.redeemed" in caplog.text
    assert invite_code not in caplog.text
    assert hash_secret(invite_code) not in caplog.text
    assert recovery_code not in caplog.text
    assert hash_secret(recovery_code) not in caplog.text


def test_concurrent_invite_redemption_has_one_success():
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    household_id = create_household(service, owner_id)
    member_id, _ = install(repository, service, BOOTSTRAP_2)
    _, code, _ = create_invite(service, owner_id, household_id)

    async def redeem_twice():
        return await asyncio.gather(
            service.redeem_invite(
                device_id=member_id, invite_code=code, network_source="203.0.113.1"
            ),
            service.redeem_invite(
                device_id=member_id, invite_code=code, network_source="203.0.113.1"
            ),
            return_exceptions=True,
        )

    outcomes = asyncio.run(redeem_twice())
    assert sum(isinstance(item, HouseholdMembership) for item in outcomes) == 1
    assert sum(isinstance(item, Exception) for item in outcomes) == 1
    assert (
        len(
            [
                member
                for (device_id, hid), member in repository.memberships.items()
                if device_id == member_id and hid == household_id and member.is_active
            ]
        )
        == 1
    )


def test_leave_and_role_changes_are_household_local_and_permission_limited():
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    first = create_household(service, owner_id)
    second = create_household(service, owner_id)
    member_id, _ = install(repository, service, BOOTSTRAP_2)
    guest_id, _ = install(repository, service, BOOTSTRAP_3)
    _, first_code, _ = create_invite(service, owner_id, first)
    _, second_code, _ = create_invite(service, owner_id, second)
    _, guest_code, _ = create_invite(service, owner_id, first)
    redeem(service, member_id, first_code)
    redeem(service, member_id, second_code)
    redeem(service, guest_id, guest_code)

    with pytest.raises(MembershipConflict):
        asyncio.run(service.leave_household(device_id=owner_id, household_id=first))
    with pytest.raises(ForbiddenIdentityAction):
        asyncio.run(
            service.set_membership_role(
                actor_device_id=member_id,
                household_id=first,
                target_device_id=owner_id,
                role=HouseholdRole.ADMIN,
            )
        )
    asyncio.run(
        service.set_membership_role(
            actor_device_id=owner_id,
            household_id=first,
            target_device_id=member_id,
            role=HouseholdRole.ADMIN,
        )
    )
    admin_invite_id, admin_invite_code, _ = asyncio.run(
        service.create_invite(actor_device_id=member_id, household_id=first)
    )
    assert admin_invite_id
    with pytest.raises(ForbiddenIdentityAction):
        asyncio.run(
            service.set_membership_role(
                actor_device_id=member_id,
                household_id=first,
                target_device_id=guest_id,
                role=HouseholdRole.ADMIN,
            )
        )
    asyncio.run(
        service.revoke_membership(
            actor_device_id=member_id,
            household_id=first,
            target_device_id=guest_id,
        )
    )
    assert admin_invite_code
    with pytest.raises(ForbiddenIdentityAction):
        asyncio.run(
            service.revoke_membership(
                actor_device_id=member_id,
                household_id=first,
                target_device_id=owner_id,
            )
        )
    asyncio.run(
        service.revoke_membership(
            actor_device_id=owner_id,
            household_id=first,
            target_device_id=member_id,
        )
    )
    asyncio.run(service.leave_household(device_id=member_id, household_id=second))
    remaining = asyncio.run(service.list_households(member_id))
    assert remaining == ()


def test_device_recovery_revokes_install_globally_and_household_recovery_is_scoped():
    repository, service, _ = make_service()
    owner_id, owner_credential = install(repository, service)
    first = create_household(service, owner_id)
    second = create_household(service, owner_id)
    assert second != first
    first_recovery = asyncio.run(
        service.issue_household_recovery(actor_device_id=owner_id, household_id=first)
    )
    recovery_code = asyncio.run(service.issue_device_recovery(owner_id))
    recovered_id, membership_count = asyncio.run(service.redeem_device_recovery(recovery_code))
    assert recovered_id == owner_id
    assert membership_count == 2
    with pytest.raises(InvalidCredential):
        asyncio.run(service.authenticate(owner_credential))
    with pytest.raises(RecoveryRejected):
        asyncio.run(service.redeem_device_recovery(recovery_code))

    replacement_id, _ = install(repository, service, BOOTSTRAP_2)
    membership = asyncio.run(
        service.redeem_household_recovery(code=first_recovery, device_id=replacement_id)
    )
    assert membership.household_id == first and membership.role is HouseholdRole.OWNER
    assert [item.household_id for item in asyncio.run(service.list_households(replacement_id))] == [
        first
    ]
    with pytest.raises(RecoveryRejected):
        asyncio.run(
            service.redeem_household_recovery(code=first_recovery, device_id=replacement_id)
        )


def test_owner_transfer_is_atomic_and_demotes_previous_owner_to_admin():
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    household_id = create_household(service, owner_id)
    member_id, _ = install(repository, service, BOOTSTRAP_2)
    _, code, _ = create_invite(service, owner_id, household_id)
    redeem(service, member_id, code)

    transferred = asyncio.run(
        service.set_membership_role(
            actor_device_id=owner_id,
            household_id=household_id,
            target_device_id=member_id,
            role=HouseholdRole.OWNER,
        )
    )
    assert transferred.role is HouseholdRole.OWNER
    assert repository.memberships[(owner_id, household_id)].role is HouseholdRole.ADMIN
    asyncio.run(service.leave_household(device_id=owner_id, household_id=household_id))
    with pytest.raises(ForbiddenIdentityAction):
        asyncio.run(
            service.revoke_membership(
                actor_device_id=owner_id,
                household_id=household_id,
                target_device_id=member_id,
            )
        )


def test_concurrent_recovery_redemption_is_single_use():
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    household_id = create_household(service, owner_id)
    code = asyncio.run(service.issue_device_recovery(owner_id))

    async def redeem_twice():
        return await asyncio.gather(
            service.redeem_device_recovery(code),
            service.redeem_device_recovery(code),
            return_exceptions=True,
        )

    outcomes = asyncio.run(redeem_twice())
    assert sum(isinstance(item, tuple) for item in outcomes) == 1
    assert sum(isinstance(item, RecoveryRejected) for item in outcomes) == 1
    assert repository.devices[owner_id].revoked_at == NOW
    assert not repository.memberships[(owner_id, household_id)].is_active


class EmptyInventory:
    async def list_items(self, household_id):
        self.household_id = household_id
        return []


def test_all_household_and_external_cost_routes_reject_anonymous_access():
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        requests = [
            ("post", "/households", {}),
            ("get", "/households", None),
            ("post", "/households/invites", {}),
            ("post", "/households/invites/redeem", {"invite_code": "x" * 40}),
            ("post", "/households/leave", {}),
            ("put", "/households/memberships/role", {"device_id": "target", "role": "member"}),
            ("post", "/households/memberships/revoke", {"device_id": "target"}),
            ("post", "/identity/device-recovery-codes", {}),
            ("post", "/households/recovery-codes", {}),
            ("post", "/households/recovery/redeem", {"recovery_code": "x" * 40}),
            ("get", "/inventory", None),
            ("get", "/shopping", None),
            ("get", "/forecast", None),
            ("get", "/forecast/reorder", None),
            ("get", "/forecast/demo-rice", None),
            (
                "post",
                "/agent/messages",
                {"household_id": "demo-household", "message": "hello"},
            ),
            (
                "post",
                "/agent/resume",
                {"household_id": "demo-household", "approval_id": "x", "approved": True},
            ),
            (
                "post",
                "/api/media/upload-authorization",
                {
                    "household_id": "demo-household",
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                    "file_size_bytes": 100,
                },
            ),
            ("put", "/staging/upload/not-a-token", None),
            ("get", "/api/commerce/options?keyword=rice", None),
        ]
        for method, path, body in requests:
            response = (
                getattr(client, method)(
                    path,
                    json=body,
                )
                if body is not None
                else getattr(client, method)(path)
            )
            assert response.status_code == 401, (method, path, response.text)

        assert (
            client.post(
                "/api/speech/transcribe",
                files={"file": ("sample.m4a", b"audio", "audio/m4a")},
            ).status_code
            == 401
        )


def test_random_bootstrap_and_device_recovery_attempts_do_not_allocate_lockouts(caplog):
    repository, service, _ = make_service()
    device_id, _ = install(repository, service)
    household_id = create_household(service, device_id)
    app = create_app()
    app.dependency_overrides[get_identity_service] = lambda: service
    before_memberships = dict(repository.memberships)
    invalid_challenge = "invalid-bootstrap-challenge-value"
    invalid_recovery = "invalid-device-recovery-code-value"

    with TestClient(app) as client:
        bootstrap_responses = [
            client.post(
                "/identity/bootstrap",
                json={"bootstrap_challenge": invalid_challenge},
            )
            for _ in range(5)
        ]
        assert [response.status_code for response in bootstrap_responses] == [401] * 5
        assert all("bearer_credential" not in response.json() for response in bootstrap_responses)
        assert set(repository.devices) == {device_id}
        assert repository.memberships == before_memberships

        recovery_responses = [
            client.post(
                "/identity/device-recovery/redeem",
                json={"recovery_code": invalid_recovery},
            )
            for _ in range(5)
        ]
        assert [response.status_code for response in recovery_responses] == [400] * 5
        assert repository.memberships == before_memberships
        assert repository.devices[device_id].revoked_at is None

        # Random challenges/codes do not allocate lockout state, so they cannot
        # deny bootstrap to two independent users with their own valid secrets.
        first = client.post(
            "/identity/bootstrap",
            json={"bootstrap_challenge": BOOTSTRAP_2},
        )
        second = client.post(
            "/identity/bootstrap",
            json={"bootstrap_challenge": BOOTSTRAP_3},
        )
        assert first.status_code == second.status_code == 200
        assert len(repository.challenge_failures) == 0

    assert household_id in repository.households
    assert invalid_challenge not in caplog.text
    assert hash_secret(invalid_challenge) not in caplog.text
    assert invalid_recovery not in caplog.text
    assert hash_secret(invalid_recovery) not in caplog.text


def test_known_bootstrap_challenge_retries_are_limited_and_remain_one_use():
    repository, service, _ = make_service()
    install(repository, service)

    with pytest.raises(InvalidBootstrapChallenge):
        asyncio.run(service.bootstrap_install(BOOTSTRAP_1))
    with pytest.raises(InvalidBootstrapChallenge):
        asyncio.run(service.bootstrap_install(BOOTSTRAP_1))
    with pytest.raises(InvalidBootstrapChallenge):
        asyncio.run(service.bootstrap_install(BOOTSTRAP_1))
    with pytest.raises(InvalidBootstrapChallenge):
        asyncio.run(service.bootstrap_install(BOOTSTRAP_1))
    with pytest.raises(ChallengeRateLimited):
        asyncio.run(service.bootstrap_install(BOOTSTRAP_1))

    assert len(repository.devices) == 1
    assert len(repository.households) == 0


def test_random_household_recovery_attempts_do_not_lock_out_known_recovery(caplog):
    repository, service, _ = make_service()
    owner_id, _ = install(repository, service)
    household_id = create_household(service, owner_id)
    recovery_code = asyncio.run(
        service.issue_household_recovery(
            actor_device_id=owner_id,
            household_id=household_id,
        )
    )
    device_recovery_code = asyncio.run(service.issue_device_recovery(owner_id))
    asyncio.run(service.redeem_device_recovery(device_recovery_code))

    app = create_app()
    app.dependency_overrides[get_identity_service] = lambda: service
    with TestClient(app) as client:
        replacement = client.post(
            "/identity/bootstrap",
            json={"bootstrap_challenge": BOOTSTRAP_2},
        )
        assert replacement.status_code == 200
        replacement_id = replacement.json()["device_id"]
        headers = {"Authorization": f"Bearer {replacement.json()['bearer_credential']}"}
        before_memberships = dict(repository.memberships)
        invalid_code = "invalid-household-recovery-code"

        responses = [
            client.post(
                "/households/recovery/redeem",
                headers=headers | {"X-Forwarded-For": f"203.0.113.{index + 1}"},
                json={"recovery_code": invalid_code},
            )
            for index in range(5)
        ]
        assert [response.status_code for response in responses] == [400] * 5
        assert repository.memberships == before_memberships
        assert repository.household_recoveries[hash_secret(recovery_code)].consumed_at is None
        restored = client.post(
            "/households/recovery/redeem",
            headers=headers,
            json={"recovery_code": recovery_code},
        )
        assert restored.status_code == 200
        assert restored.json() == {
            "household_id": household_id,
            "device_id": replacement_id,
            "role": "owner",
        }
        assert repository.memberships[(replacement_id, household_id)].role is HouseholdRole.OWNER
        assert repository.household_recoveries[hash_secret(recovery_code)].consumed_at is not None
        replay = client.post(
            "/households/recovery/redeem",
            headers=headers,
            json={"recovery_code": recovery_code},
        )
        assert replay.status_code == 400

    assert invalid_code not in caplog.text
    assert hash_secret(invalid_code) not in caplog.text
    assert recovery_code not in caplog.text
    assert hash_secret(recovery_code) not in caplog.text


def test_household_data_routes_require_auth_and_reject_cross_household_scope():
    repository, service, _ = make_service()
    device_id, credential = install(repository, service)
    first = create_household(service, device_id)
    second = create_household(service, device_id)
    inventory = EmptyInventory()
    app = create_app()
    app.dependency_overrides[get_identity_service] = lambda: service
    app.dependency_overrides[get_inventory_service] = lambda: inventory
    app.dependency_overrides[get_read_agent] = lambda: None
    app.dependency_overrides[get_approval_service] = lambda: None
    bearer = {"Authorization": f"Bearer {credential}"}
    first_scope = bearer | {"X-Household-Id": first}
    other_scope = bearer | {"X-Household-Id": "not-a-member"}

    with TestClient(app) as client:
        assert client.get("/inventory", headers={"X-Household-Id": first}).status_code == 401
        assert client.get("/inventory", headers=first_scope).status_code == 200
        assert inventory.household_id == first
        assert client.get("/inventory", headers=bearer).status_code == 400
        assert client.get("/shopping", headers=other_scope).status_code == 403
        for path in (
            "/inventory",
            "/shopping",
            "/forecast",
            "/forecast/reorder",
            "/forecast/demo-rice",
        ):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=other_scope).status_code == 403
        assert client.post("/households", json={}).status_code == 401
        assert client.get("/households", headers=bearer).status_code == 200
        assert client.get("/households", headers=bearer).json()["households"] == [
            {"household_id": first, "role": "owner"},
            {"household_id": second, "role": "owner"},
        ]
        assert (
            client.post(
                "/agent/messages",
                headers=first_scope,
                json={"household_id": second, "message": "hello"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/agent/resume",
                headers=first_scope,
                json={"household_id": second, "approval_id": "approval-x", "approved": True},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/media/upload-authorization",
                headers=first_scope,
                json={
                    "household_id": second,
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                    "file_size_bytes": 100,
                },
            ).status_code
            == 403
        )
        cross_scope_mutations = [
            ("post", "/households/invites", {}),
            ("post", "/households/leave", {}),
            (
                "put",
                "/households/memberships/role",
                {"device_id": device_id, "role": "admin"},
            ),
            (
                "post",
                "/households/memberships/revoke",
                {"device_id": device_id},
            ),
            ("post", "/households/recovery-codes", {}),
            (
                "post",
                "/api/media/upload-authorization",
                {
                    "household_id": first,
                    "filename": "photo.jpg",
                    "content_type": "image/jpeg",
                    "file_size_bytes": 100,
                },
            ),
            (
                "post",
                "/agent/messages",
                {"household_id": first, "message": "hello"},
            ),
            (
                "post",
                "/agent/resume",
                {"household_id": first, "approval_id": "approval-x", "approved": True},
            ),
        ]
        before_memberships = dict(repository.memberships)
        before_invites = dict(repository.invites)
        for method, path, body in cross_scope_mutations:
            response = getattr(client, method)(
                path,
                headers=other_scope,
                json=body,
            )
            assert response.status_code == 403, (method, path, response.text)
        assert repository.memberships == before_memberships
        assert repository.invites == before_invites
        assert (
            client.put(
                "/staging/upload/not-a-token",
                headers=other_scope,
                content=b"payload",
            ).status_code
            == 403
        )
        assert repository.memberships == before_memberships


def test_identity_http_flows_bootstrap_create_invite_join_and_switch():
    repository, service, _ = make_service()
    owner_id, owner_credential = install(repository, service)
    app = create_app()
    app.dependency_overrides[get_identity_service] = lambda: service
    with TestClient(app) as client:
        owner_headers = {"Authorization": f"Bearer {owner_credential}"}
        created = client.post("/households", headers=owner_headers, json={})
        assert created.status_code == 200
        household_id = created.json()["household_id"]
        owner_scope = owner_headers | {"X-Household-Id": household_id}

        other_install = client.post(
            "/identity/bootstrap",
            json={"bootstrap_challenge": BOOTSTRAP_2},
        )
        assert other_install.status_code == 200
        member_id = other_install.json()["device_id"]
        member_token = other_install.json()["bearer_credential"]
        assert repository.devices[member_id].credential_hash == hash_secret(member_token)

        invite = client.post("/households/invites", headers=owner_scope, json={})
        assert invite.status_code == 200
        assert invite.json()["household_id"] == household_id
        assert hash_secret(invite.json()["invite_code"]) in repository.invites

        member_headers = {"Authorization": f"Bearer {member_token}"}
        redeemed = client.post(
            "/households/invites/redeem",
            headers=member_headers,
            json={"invite_code": invite.json()["invite_code"]},
        )
        assert redeemed.status_code == 200
        assert redeemed.json()["household_id"] == household_id
        listed = client.get("/households", headers=member_headers)
        assert listed.json()["households"] == [{"household_id": household_id, "role": "member"}]
        assert (
            client.get(
                "/inventory",
                headers=member_headers | {"X-Household-Id": household_id},
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/inventory",
                headers=member_headers | {"X-Household-Id": "different-household"},
            ).status_code
            == 403
        )
        role_change = client.put(
            "/households/memberships/role",
            headers=owner_scope,
            json={"device_id": member_id, "role": "admin"},
        )
        assert role_change.status_code == 200
        assert role_change.json()["role"] == "admin"
        assert (
            client.post(
                "/households/invites",
                headers=member_headers | {"X-Household-Id": household_id},
                json={},
            ).status_code
            == 200
        )
        revoked = client.post(
            "/households/memberships/revoke",
            headers=owner_scope,
            json={"device_id": member_id},
        )
        assert revoked.status_code == 200
        assert (
            client.get(
                "/inventory",
                headers=member_headers | {"X-Household-Id": household_id},
            ).status_code
            == 403
        )


def test_recovery_endpoints_revoke_install_and_restore_one_household():
    repository, service, _ = make_service()
    owner_id, owner_credential = install(repository, service)
    first = create_household(service, owner_id)
    second = create_household(service, owner_id)
    app = create_app()
    app.dependency_overrides[get_identity_service] = lambda: service
    with TestClient(app) as client:
        owner_headers = {"Authorization": f"Bearer {owner_credential}"}
        recovery_scope = owner_headers | {"X-Household-Id": first}
        household_code = client.post(
            "/households/recovery-codes",
            headers=recovery_scope,
            json={},
        )
        assert household_code.status_code == 200
        device_code = client.post(
            "/identity/device-recovery-codes",
            headers=owner_headers,
            json={},
        )
        assert device_code.status_code == 200

        revoked = client.post(
            "/identity/device-recovery/redeem",
            json={"recovery_code": device_code.json()["recovery_code"]},
        )
        assert revoked.status_code == 200
        assert revoked.json() == {
            "revoked_device_id": owner_id,
            "revoked_membership_count": 2,
        }
        replacement = client.post(
            "/identity/bootstrap",
            json={"bootstrap_challenge": BOOTSTRAP_2},
        )
        assert replacement.status_code == 200
        replacement_headers = {"Authorization": f"Bearer {replacement.json()['bearer_credential']}"}
        restored = client.post(
            "/households/recovery/redeem",
            headers=replacement_headers,
            json={"recovery_code": household_code.json()["recovery_code"]},
        )
        assert restored.status_code == 200
        assert restored.json()["household_id"] == first
        listed = client.get("/households", headers=replacement_headers)
        assert listed.json()["households"] == [{"household_id": first, "role": "owner"}]
        assert second not in {item["household_id"] for item in listed.json()["households"]}


def test_media_staging_upload_token_is_bound_to_selected_household():
    repository, service, _ = make_service()
    device_id, credential = install(repository, service)
    first = create_household(service, device_id)
    second = create_household(service, device_id)
    storage = LocalStagingMediaStorage(auto_process=False)
    app = create_app()
    app.dependency_overrides[get_identity_service] = lambda: service
    app.dependency_overrides[get_media_storage_service] = lambda: storage
    headers = {"Authorization": f"Bearer {credential}", "X-Household-Id": first}

    with TestClient(app) as client:
        authorization = client.post(
            "/api/media/upload-authorization",
            headers=headers,
            json={
                "household_id": first,
                "filename": "photo.jpg",
                "content_type": "image/jpeg",
                "file_size_bytes": 100,
            },
        )
        assert authorization.status_code == 200
        token = authorization.json()["upload_url"].rsplit("/", 1)[-1]
        payload = b"image bytes"
        assert (
            client.put(
                f"/staging/upload/{token}",
                headers=headers | {"X-Household-Id": second},
                content=payload,
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"/staging/upload/{token}",
                headers=headers,
                content=payload,
            ).status_code
            == 201
        )


def test_runtime_without_identity_repository_fails_closed():
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/inventory").status_code == 401
        assert (
            client.get(
                "/inventory",
                headers={
                    "Authorization": "Bearer " + ("c" * 48),
                    "X-Household-Id": "demo-household",
                },
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/identity/bootstrap",
                json={"bootstrap_challenge": BOOTSTRAP_1},
            ).status_code
            == 503
        )
