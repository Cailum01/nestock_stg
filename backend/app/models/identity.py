"""Domain contracts for install identity and household membership.

These records do not authorize requests or implement membership lifecycle flows.
"""

import base64
import binascii
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.models.base import ContractModel

HASH_PATTERN = r"^[0-9a-f]{64}$"
HTTP_ID_MAX_LENGTH = 128
ONE_TIME_SECRET_MIN_LENGTH = 16
ONE_TIME_SECRET_MAX_LENGTH = 512


class HouseholdRecord(ContractModel):
    """A household and the install that created it."""

    household_id: str = Field(min_length=1)
    created_by_device_id: str = Field(min_length=1)
    created_at: datetime


class DeviceRecord(ContractModel):
    """Per-install identity; only a credential hash belongs in this record."""

    device_id: str = Field(min_length=1)
    credential_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime
    default_household_id: str | None = Field(default=None, min_length=1)
    revoked_at: datetime | None = None


class HouseholdRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class HouseholdMembership(ContractModel):
    """One device's role in one household; roles are never device-global."""

    household_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    role: HouseholdRole
    created_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class HouseholdCreation(ContractModel):
    """Creation result paired with the creating install's initial owner membership."""

    household: HouseholdRecord
    owner_membership: HouseholdMembership

    @model_validator(mode="after")
    def validate_initial_owner(self) -> "HouseholdCreation":
        if self.owner_membership.household_id != self.household.household_id:
            raise ValueError("Initial owner membership must target the created household.")
        if self.owner_membership.device_id != self.household.created_by_device_id:
            raise ValueError("Initial owner membership must belong to the creating device.")
        if self.owner_membership.role is not HouseholdRole.OWNER:
            raise ValueError("Household creation requires an initial owner membership.")
        return self


class DeviceMemberships(ContractModel):
    """A device's household membership view, with one record per household."""

    device_id: str = Field(min_length=1)
    memberships: tuple[HouseholdMembership, ...] = ()

    @model_validator(mode="after")
    def validate_memberships(self) -> "DeviceMemberships":
        if any(membership.device_id != self.device_id for membership in self.memberships):
            raise ValueError("Every membership must belong to this device.")
        household_ids = [membership.household_id for membership in self.memberships]
        if len(set(household_ids)) != len(household_ids):
            raise ValueError("A device may have only one membership record per household.")
        return self


class RequestedHouseholdContext(ContractModel):
    """Untrusted selected scope; it is not evidence of household membership."""

    requested_household_id: str = Field(min_length=1)


class HouseholdInviteRecord(ContractModel):
    """A household-scoped, one-use invite record; the bearer value is never stored."""

    invite_id: str = Field(min_length=1)
    household_id: str = Field(min_length=1)
    token_hash: str = Field(pattern=HASH_PATTERN)
    created_by_device_id: str = Field(min_length=1)
    granted_role: Literal[HouseholdRole.MEMBER] = HouseholdRole.MEMBER
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None = None
    redeemed_by_device_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_invite_lifecycle(self) -> "HouseholdInviteRecord":
        if self.expires_at <= self.created_at:
            raise ValueError("Invite expiry must be after creation.")
        if (self.redeemed_at is None) != (self.redeemed_by_device_id is None):
            raise ValueError("Invite redemption time and device must be recorded together.")
        return self


class DeviceRecoveryRecord(ContractModel):
    """Single-use recovery material for globally revoking one install."""

    recovery_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    code_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime
    consumed_at: datetime | None = None


class HouseholdRecoveryRecord(ContractModel):
    """Single-use recovery material scoped to owner membership in one household."""

    recovery_id: str = Field(min_length=1)
    household_id: str = Field(min_length=1)
    code_hash: str = Field(pattern=HASH_PATTERN)
    created_at: datetime
    consumed_at: datetime | None = None


class HouseholdOperationIdentity(ContractModel):
    """Immutable household-scoped idempotency identity.

    Persistence must reject rebinding an operation_id to a different household.
    """

    household_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)


class HouseholdContextHeader(ContractModel):
    """Untrusted per-request ``X-Household-Id`` selection, never authorization."""

    requested_household_id: str = Field(
        alias="X-Household-Id",
        min_length=1,
        max_length=HTTP_ID_MAX_LENGTH,
    )


class BootstrapInstallRequest(ContractModel):
    """One-time bootstrap challenge for issuing an install credential."""

    bootstrap_challenge: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class BootstrapInstallResponse(ContractModel):
    """One-time plaintext bearer issuance; never persist or log this response."""

    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    bearer_credential: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class GoogleIdentityRequest(ContractModel):
    """Untrusted compact Google ID token; verification is a server-side concern."""

    google_id_token: str = Field(min_length=1, max_length=8192, repr=False)

    @field_validator("google_id_token")
    @classmethod
    def validate_compact_token(cls, value: str) -> str:
        """Check JWT envelope syntax only; this does not authenticate the token."""
        segments = value.split(".")
        if len(segments) != 3 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", segment) for segment in segments
        ):
            raise ValueError("Google ID token must be a compact JWT.")

        decoded: list[bytes] = []
        for segment in segments:
            padded = segment + "=" * (-len(segment) % 4)
            try:
                decoded.append(
                    base64.b64decode(
                        padded,
                        altchars=b"-_",
                        validate=True,
                    )
                )
            except (binascii.Error, ValueError) as error:
                raise ValueError("Google ID token must be a compact JWT.") from error

        try:
            header = json.loads(decoded[0])
            claims = json.loads(decoded[1])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Google ID token must contain JSON header and claims.") from error
        if not isinstance(header, dict) or not isinstance(claims, dict) or not decoded[2]:
            raise ValueError("Google ID token must contain a header, claims, and signature.")
        return value


class GoogleIdentityResponse(ContractModel):
    """Per-install bearer and its verified account's default household."""

    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    bearer_credential: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class IdentitySessionResponse(ContractModel):
    """Authenticated install's server-persisted default household scope."""

    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)


class BootstrapChallengeResponse(ContractModel):
    """One-time operator-issued challenge; never persist or log the plaintext."""

    bootstrap_challenge: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class CreateHouseholdRequest(ContractModel):
    """Creates a server-ID household for the credential's install."""


class CreateHouseholdResponse(ContractModel):
    """Public creation result; contains no storage record or credential hash."""

    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    role: Literal[HouseholdRole.OWNER] = HouseholdRole.OWNER


class ListHouseholdsRequest(ContractModel):
    """Lists the authenticated install's active memberships."""


class HouseholdSummary(ContractModel):
    """Public household and per-membership role view."""

    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    role: HouseholdRole


class ListHouseholdsResponse(ContractModel):
    """An empty list is valid for a newly bootstrapped install."""

    households: tuple[HouseholdSummary, ...] = ()


class CreateInviteRequest(ContractModel):
    """Creates a member invite in the selected household context."""


class CreateInviteResponse(ContractModel):
    """One-time plaintext invite issuance; never persist or log this response."""

    invite_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    invite_code: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )
    expires_at: datetime


class RedeemInviteRequest(ContractModel):
    """Redeems a one-time invite; the invite determines its household."""

    invite_code: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class RedeemInviteResponse(ContractModel):
    """Membership added by invite redemption; existing memberships remain."""

    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    role: Literal[HouseholdRole.MEMBER] = HouseholdRole.MEMBER


class LeaveHouseholdRequest(ContractModel):
    """Leaves only the household selected by the request context."""


class LeaveHouseholdResponse(ContractModel):
    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    left: Literal[True] = True


class SetMembershipRoleRequest(ContractModel):
    """Requests a per-household role change; authorization is server-side."""

    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    role: HouseholdRole


class SetMembershipRoleResponse(ContractModel):
    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    role: HouseholdRole


class RevokeMembershipRequest(ContractModel):
    """Revokes one target install's membership in the selected household."""

    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)


class RevokeMembershipResponse(ContractModel):
    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    revoked: Literal[True] = True


class IssueDeviceRecoveryCodeRequest(ContractModel):
    """Requests a new one-time recovery code for the authenticated install."""


class IssueDeviceRecoveryCodeResponse(ContractModel):
    """One-time plaintext device recovery code; never persist or log this response."""

    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    recovery_code: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class RedeemDeviceRecoveryRequest(ContractModel):
    """Redeems a lost-install code, globally revoking that install and memberships."""

    recovery_code: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class RedeemDeviceRecoveryResponse(ContractModel):
    revoked_device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    revoked_membership_count: int = Field(ge=0)


class IssueHouseholdRecoveryCodeRequest(ContractModel):
    """Requests a one-time owner recovery code for the selected household."""


class IssueHouseholdRecoveryCodeResponse(ContractModel):
    """One-time plaintext household recovery code; never persist or log this response."""

    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    recovery_code: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class RedeemHouseholdRecoveryRequest(ContractModel):
    """Redeems one household recovery code for the authenticated replacement install."""

    recovery_code: str = Field(
        min_length=ONE_TIME_SECRET_MIN_LENGTH,
        max_length=ONE_TIME_SECRET_MAX_LENGTH,
        repr=False,
    )


class RedeemHouseholdRecoveryResponse(ContractModel):
    household_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    device_id: str = Field(min_length=1, max_length=HTTP_ID_MAX_LENGTH)
    role: Literal[HouseholdRole.OWNER] = HouseholdRole.OWNER
