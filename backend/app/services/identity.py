"""Identity lifecycle rules backed by an atomic repository port.

The application intentionally provides no production identity repository yet.
WO-007 owns adapter composition; callers must fail closed until one is supplied.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.models.identity import (
    DeviceMemberships,
    DeviceRecord,
    DeviceRecoveryRecord,
    HouseholdCreation,
    HouseholdInviteRecord,
    HouseholdMembership,
    HouseholdRecoveryRecord,
    HouseholdRole,
    HouseholdSummary,
)

INVITE_LIFETIME = timedelta(minutes=10)
INVITES_PER_HOUSEHOLD_PER_HOUR = 10
INVITE_FAILURE_LIMIT = 5
INVITE_FAILURE_WINDOW = timedelta(minutes=15)
INVITE_COOLDOWN = timedelta(minutes=15)
CHALLENGE_FAILURE_LIMIT = INVITE_FAILURE_LIMIT
CHALLENGE_FAILURE_WINDOW = INVITE_FAILURE_WINDOW
CHALLENGE_COOLDOWN = INVITE_COOLDOWN
RECOVERY_SECRET_BYTES = 32
HOUSEHOLD_CREATIONS_PER_HOUR = 3
HOUSEHOLD_CREATION_WINDOW = timedelta(hours=1)
logger = logging.getLogger(__name__)


class IdentityError(Exception):
    """Safe-to-return identity error with an HTTP-neutral status code."""

    status_code = 400
    public_detail = "Identity request was rejected."

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.public_detail)
        self.detail = detail or self.public_detail


class InvalidCredential(IdentityError):
    status_code = 401
    public_detail = "Invalid install credential."


class InvalidBootstrapChallenge(IdentityError):
    status_code = 401
    public_detail = "Bootstrap challenge is invalid or already used."


class ForbiddenIdentityAction(IdentityError):
    status_code = 403
    public_detail = "This install is not authorized for that household action."


class HouseholdScopeMismatch(IdentityError):
    status_code = 403
    public_detail = "Request household does not match the authorized household context."


class InvalidInvite(IdentityError):
    status_code = 400
    public_detail = "Invite is invalid, expired, or already redeemed."


class InviteRateLimited(IdentityError):
    status_code = 429
    public_detail = "Invite rate limit exceeded."


class ChallengeRateLimited(IdentityError):
    status_code = 429
    public_detail = "Challenge rate limit exceeded."


class HouseholdCreationRateLimited(IdentityError):
    status_code = 429
    public_detail = "Household creation rate limit exceeded."


class MembershipConflict(IdentityError):
    status_code = 409
    public_detail = "Membership transition is not allowed."


class RecoveryRejected(IdentityError):
    status_code = 400
    public_detail = "Recovery code is invalid, expired, or already used."


class InviteRedemptionRejected(Exception):
    """Repository result for an invalid, expired, or previously redeemed invite."""


class IdentityRepository(Protocol):
    """Atomic identity persistence boundary; implementations are owned by WO-007.

    Methods performing authorization and mutation must validate the actor and
    update the associated records in one transaction. In particular,
    ``redeem_invite`` must create the membership and consume the invite
    atomically, and ``redeem_device_recovery`` must revoke the device and all
    of its memberships atomically. ``bootstrap_install`` must consume a
    one-time challenge while registering a unique credential hash;
    ``create_invite`` must recheck the actor role and household invite limit
    with invite creation. Role transfer to ``owner`` must atomically assign the
    target as owner and demote the actor to admin. The adapter must reject
    operations whose expected actor state changed after service preflight.
    Issuing device/household recovery must replace any prior unconsumed code
    in that same scope; recovery redemption must consume the code atomically
    with its revocation or owner-membership change.
    """

    async def bootstrap_install(
        self,
        *,
        challenge_hash: str,
        device: DeviceRecord,
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
    ) -> bool: ...

    async def find_active_device_by_credential_hash(
        self, credential_hash: str
    ) -> DeviceRecord | None: ...

    async def create_household(
        self,
        *,
        device_id: str,
        creation: HouseholdCreation,
        now: datetime,
        max_per_hour: int,
        window: timedelta,
    ) -> None: ...

    async def list_device_memberships(self, device_id: str) -> DeviceMemberships: ...

    async def get_active_membership(
        self, *, device_id: str, household_id: str
    ) -> HouseholdMembership | None: ...

    async def create_invite(
        self,
        *,
        actor_device_id: str,
        invite: HouseholdInviteRecord,
        now: datetime,
        max_per_hour: int,
    ) -> None: ...

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
        """Atomically check rate limits, consume one invite, and add membership."""
        ...

    async def leave_household(
        self, *, device_id: str, household_id: str, now: datetime
    ) -> None: ...

    async def set_membership_role(
        self,
        *,
        actor_device_id: str,
        household_id: str,
        target_device_id: str,
        role: HouseholdRole,
        now: datetime,
    ) -> HouseholdMembership: ...

    async def revoke_membership(
        self,
        *,
        actor_device_id: str,
        household_id: str,
        target_device_id: str,
        now: datetime,
    ) -> None: ...

    async def issue_device_recovery(
        self, *, actor_device_id: str, recovery: DeviceRecoveryRecord
    ) -> None: ...

    async def redeem_device_recovery(
        self,
        *,
        code_hash: str,
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
    ) -> tuple[str, int]: ...

    async def issue_household_recovery(
        self, *, actor_device_id: str, recovery: HouseholdRecoveryRecord
    ) -> None: ...

    async def redeem_household_recovery(
        self,
        *,
        code_hash: str,
        device_id: str,
        now: datetime,
        failure_limit: int,
        failure_window: timedelta,
        cooldown: timedelta,
    ) -> HouseholdMembership: ...


class IdentityService:
    """Business rules for install credentials, household membership and recovery.

    Google token exchange is a separate authentication boundary: it may establish
    only the per-install bearer and must not create households, memberships, or
    redeem invites. Provider verification and exchange wiring are owned elsewhere.
    """

    def __init__(
        self,
        repository: IdentityRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def bootstrap_install(
        self,
        challenge: str,
    ) -> tuple[str, str]:
        now = self._clock()
        credential = secrets.token_urlsafe(RECOVERY_SECRET_BYTES)
        device = DeviceRecord(
            device_id=uuid.uuid4().hex,
            credential_hash=hash_secret(credential),
            created_at=now,
        )
        accepted = await self._repository.bootstrap_install(
            challenge_hash=hash_secret(challenge),
            device=device,
            now=now,
            failure_limit=CHALLENGE_FAILURE_LIMIT,
            failure_window=CHALLENGE_FAILURE_WINDOW,
            cooldown=CHALLENGE_COOLDOWN,
        )
        if not accepted:
            raise InvalidBootstrapChallenge()
        logger.info("identity.install.issued device_id=%s", device.device_id)
        return device.device_id, credential

    async def authenticate(self, credential: str) -> DeviceRecord:
        device = await self._repository.find_active_device_by_credential_hash(
            hash_secret(credential)
        )
        if device is None or device.revoked_at is not None:
            raise InvalidCredential()
        return device

    async def authorize_household(
        self, *, device_id: str, household_id: str
    ) -> HouseholdMembership:
        membership = await self._repository.get_active_membership(
            device_id=device_id,
            household_id=household_id,
        )
        if membership is None or not membership.is_active:
            raise ForbiddenIdentityAction()
        return membership

    @staticmethod
    def require_matching_household(requested: str, authorized: str) -> None:
        if requested != authorized:
            raise HouseholdScopeMismatch()

    async def create_household(self, device_id: str) -> str:
        household_id = uuid.uuid4().hex
        now = self._clock()
        creation = HouseholdCreation(
            household={
                "household_id": household_id,
                "created_by_device_id": device_id,
                "created_at": now,
            },
            owner_membership={
                "household_id": household_id,
                "device_id": device_id,
                "role": HouseholdRole.OWNER,
                "created_at": now,
            },
        )
        await self._repository.create_household(
            device_id=device_id,
            creation=creation,
            now=now,
            max_per_hour=HOUSEHOLD_CREATIONS_PER_HOUR,
            window=HOUSEHOLD_CREATION_WINDOW,
        )
        logger.info(
            "identity.household.created household_id=%s device_id=%s",
            household_id,
            device_id,
        )
        return household_id

    async def list_households(self, device_id: str) -> tuple[HouseholdSummary, ...]:
        memberships = await self._repository.list_device_memberships(device_id)
        return tuple(
            HouseholdSummary(household_id=item.household_id, role=item.role)
            for item in memberships.memberships
            if item.is_active
        )

    async def create_invite(
        self, *, actor_device_id: str, household_id: str
    ) -> tuple[str, str, datetime]:
        actor = await self.authorize_household(
            device_id=actor_device_id,
            household_id=household_id,
        )
        if actor.role not in (HouseholdRole.OWNER, HouseholdRole.ADMIN):
            raise ForbiddenIdentityAction()
        now = self._clock()
        invite_code = secrets.token_urlsafe(RECOVERY_SECRET_BYTES)
        expires_at = now + INVITE_LIFETIME
        invite = HouseholdInviteRecord(
            invite_id=uuid.uuid4().hex,
            household_id=household_id,
            token_hash=hash_secret(invite_code),
            created_by_device_id=actor_device_id,
            created_at=now,
            expires_at=expires_at,
        )
        await self._repository.create_invite(
            actor_device_id=actor_device_id,
            invite=invite,
            now=now,
            max_per_hour=INVITES_PER_HOUSEHOLD_PER_HOUR,
        )
        logger.info(
            "identity.invite.created household_id=%s invite_id=%s actor_device_id=%s",
            household_id,
            invite.invite_id,
            actor_device_id,
        )
        return invite.invite_id, invite_code, expires_at

    async def redeem_invite(
        self, *, device_id: str, invite_code: str, network_source: str
    ) -> HouseholdMembership:
        try:
            membership = await self._repository.redeem_invite(
                token_hash=hash_secret(invite_code),
                device_id=device_id,
                network_key=hash_secret(network_source),
                now=self._clock(),
                failure_limit=INVITE_FAILURE_LIMIT,
                failure_window=INVITE_FAILURE_WINDOW,
                cooldown=INVITE_COOLDOWN,
            )
        except InviteRateLimited:
            logger.warning("identity.invite.redeem rate_limited device_id=%s", device_id)
            raise
        except InviteRedemptionRejected as exc:
            logger.info("identity.invite.redeem rejected device_id=%s", device_id)
            raise InvalidInvite() from exc
        logger.info(
            "identity.invite.redeemed household_id=%s device_id=%s",
            membership.household_id,
            device_id,
        )
        return membership

    async def leave_household(self, *, device_id: str, household_id: str) -> None:
        membership = await self.authorize_household(
            device_id=device_id,
            household_id=household_id,
        )
        if membership.role is HouseholdRole.OWNER:
            raise MembershipConflict("Transfer ownership before leaving.")
        await self._repository.leave_household(
            device_id=device_id,
            household_id=household_id,
            now=self._clock(),
        )
        logger.info(
            "identity.membership.left household_id=%s device_id=%s",
            household_id,
            device_id,
        )

    async def set_membership_role(
        self,
        *,
        actor_device_id: str,
        household_id: str,
        target_device_id: str,
        role: HouseholdRole,
    ) -> HouseholdMembership:
        actor = await self.authorize_household(
            device_id=actor_device_id,
            household_id=household_id,
        )
        target = await self.authorize_household(
            device_id=target_device_id,
            household_id=household_id,
        )
        if actor.role is HouseholdRole.OWNER:
            if target.role is HouseholdRole.OWNER:
                raise MembershipConflict("A household can have only one owner.")
            if role is HouseholdRole.OWNER and actor_device_id == target_device_id:
                raise MembershipConflict("Transfer ownership to another member.")
        elif actor.role is HouseholdRole.ADMIN:
            if target.role is not HouseholdRole.MEMBER or role is not HouseholdRole.MEMBER:
                raise ForbiddenIdentityAction()
        else:
            raise ForbiddenIdentityAction()
        membership = await self._repository.set_membership_role(
            actor_device_id=actor_device_id,
            household_id=household_id,
            target_device_id=target_device_id,
            role=role,
            now=self._clock(),
        )
        logger.info(
            "identity.membership.role_changed household_id=%s actor_device_id=%s "
            "target_device_id=%s role=%s",
            household_id,
            actor_device_id,
            target_device_id,
            membership.role,
        )
        return membership

    async def revoke_membership(
        self, *, actor_device_id: str, household_id: str, target_device_id: str
    ) -> None:
        actor = await self.authorize_household(
            device_id=actor_device_id,
            household_id=household_id,
        )
        target = await self.authorize_household(
            device_id=target_device_id,
            household_id=household_id,
        )
        if actor_device_id == target_device_id:
            raise ForbiddenIdentityAction()
        if actor.role is HouseholdRole.OWNER:
            allowed = target.role is not HouseholdRole.OWNER
        elif actor.role is HouseholdRole.ADMIN:
            allowed = target.role is HouseholdRole.MEMBER
        else:
            allowed = False
        if not allowed:
            raise ForbiddenIdentityAction()
        await self._repository.revoke_membership(
            actor_device_id=actor_device_id,
            household_id=household_id,
            target_device_id=target_device_id,
            now=self._clock(),
        )
        logger.info(
            "identity.membership.revoked household_id=%s actor_device_id=%s target_device_id=%s",
            household_id,
            actor_device_id,
            target_device_id,
        )

    async def issue_device_recovery(self, device_id: str) -> str:
        code = secrets.token_urlsafe(RECOVERY_SECRET_BYTES)
        recovery = DeviceRecoveryRecord(
            recovery_id=uuid.uuid4().hex,
            device_id=device_id,
            code_hash=hash_secret(code),
            created_at=self._clock(),
        )
        await self._repository.issue_device_recovery(
            actor_device_id=device_id,
            recovery=recovery,
        )
        logger.info("identity.device_recovery.issued device_id=%s", device_id)
        return code

    async def redeem_device_recovery(
        self,
        code: str,
    ) -> tuple[str, int]:
        try:
            result = await self._repository.redeem_device_recovery(
                code_hash=hash_secret(code),
                now=self._clock(),
                failure_limit=CHALLENGE_FAILURE_LIMIT,
                failure_window=CHALLENGE_FAILURE_WINDOW,
                cooldown=CHALLENGE_COOLDOWN,
            )
        except ChallengeRateLimited:
            logger.warning("identity.device_recovery.rate_limited")
            raise
        except InviteRedemptionRejected as exc:
            logger.info("identity.device_recovery.rejected")
            raise RecoveryRejected() from exc
        logger.warning(
            "identity.device_recovery.redeemed device_id=%s revoked_memberships=%s",
            result[0],
            result[1],
        )
        return result

    async def issue_household_recovery(self, *, actor_device_id: str, household_id: str) -> str:
        actor = await self.authorize_household(
            device_id=actor_device_id,
            household_id=household_id,
        )
        if actor.role is not HouseholdRole.OWNER:
            raise ForbiddenIdentityAction()
        code = secrets.token_urlsafe(RECOVERY_SECRET_BYTES)
        recovery = HouseholdRecoveryRecord(
            recovery_id=uuid.uuid4().hex,
            household_id=household_id,
            code_hash=hash_secret(code),
            created_at=self._clock(),
        )
        await self._repository.issue_household_recovery(
            actor_device_id=actor_device_id,
            recovery=recovery,
        )
        logger.info(
            "identity.household_recovery.issued household_id=%s actor_device_id=%s",
            household_id,
            actor_device_id,
        )
        return code

    async def redeem_household_recovery(
        self,
        *,
        code: str,
        device_id: str,
    ) -> HouseholdMembership:
        try:
            membership = await self._repository.redeem_household_recovery(
                code_hash=hash_secret(code),
                device_id=device_id,
                now=self._clock(),
                failure_limit=CHALLENGE_FAILURE_LIMIT,
                failure_window=CHALLENGE_FAILURE_WINDOW,
                cooldown=CHALLENGE_COOLDOWN,
            )
        except ChallengeRateLimited:
            logger.warning(
                "identity.household_recovery.rate_limited device_id=%s",
                device_id,
            )
            raise
        except InviteRedemptionRejected as exc:
            logger.info("identity.household_recovery.rejected")
            raise RecoveryRejected() from exc
        logger.warning(
            "identity.household_recovery.redeemed household_id=%s device_id=%s",
            membership.household_id,
            device_id,
        )
        return membership


def hash_secret(value: str) -> str:
    """Return a lowercase SHA-256 hex digest without retaining the source value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
