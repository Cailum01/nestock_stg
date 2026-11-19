from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.agent import ApprovalPresentation
from app.models.identity import (
    BootstrapInstallRequest,
    BootstrapInstallResponse,
    CreateHouseholdRequest,
    CreateHouseholdResponse,
    CreateInviteRequest,
    CreateInviteResponse,
    DeviceMemberships,
    DeviceRecord,
    DeviceRecoveryRecord,
    HouseholdContextHeader,
    HouseholdCreation,
    HouseholdInviteRecord,
    HouseholdMembership,
    HouseholdOperationIdentity,
    HouseholdRecord,
    HouseholdRecoveryRecord,
    HouseholdRole,
    HouseholdSummary,
    IssueDeviceRecoveryCodeRequest,
    IssueDeviceRecoveryCodeResponse,
    IssueHouseholdRecoveryCodeRequest,
    IssueHouseholdRecoveryCodeResponse,
    LeaveHouseholdRequest,
    LeaveHouseholdResponse,
    ListHouseholdsRequest,
    ListHouseholdsResponse,
    RedeemDeviceRecoveryRequest,
    RedeemDeviceRecoveryResponse,
    RedeemHouseholdRecoveryRequest,
    RedeemHouseholdRecoveryResponse,
    RedeemInviteRequest,
    RedeemInviteResponse,
    RequestedHouseholdContext,
    RevokeMembershipRequest,
    RevokeMembershipResponse,
    SetMembershipRoleRequest,
    SetMembershipRoleResponse,
)
from app.services.approvals import ApprovalRecord, ApprovalStatus, StoredOperation

NOW = datetime(2026, 11, 19, 0, 0, tzinfo=UTC)
HASH = "a" * 64


def test_device_household_and_membership_contracts_are_install_scoped():
    household = HouseholdRecord(
        household_id="household-a",
        created_by_device_id="install-1",
        created_at=NOW,
    )
    device = DeviceRecord(device_id="install-1", credential_hash=HASH, created_at=NOW)
    owner_membership = HouseholdMembership(
        household_id=household.household_id,
        device_id=device.device_id,
        role=HouseholdRole.OWNER,
        created_at=NOW,
    )
    creation = HouseholdCreation(household=household, owner_membership=owner_membership)
    assert creation.owner_membership.role is HouseholdRole.OWNER

    existing_memberships = DeviceMemberships(
        device_id=device.device_id,
        memberships=(owner_membership,),
    )
    memberships = DeviceMemberships(
        device_id=device.device_id,
        memberships=existing_memberships.memberships
        + (
            HouseholdMembership(
                household_id="household-b",
                device_id=device.device_id,
                role=HouseholdRole.MEMBER,
                created_at=NOW,
            ),
        ),
    )

    assert len(memberships.memberships) == 2
    assert [membership.role for membership in memberships.memberships] == [
        HouseholdRole.OWNER,
        HouseholdRole.MEMBER,
    ]
    assert all(membership.is_active for membership in memberships.memberships)
    assert memberships.memberships[0] == existing_memberships.memberships[0]
    assert RequestedHouseholdContext(requested_household_id="household-b")
    assert DeviceMemberships(device_id=device.device_id).memberships == ()

    with pytest.raises(ValidationError):
        HouseholdCreation(
            household=household,
            owner_membership=HouseholdMembership(
                household_id="household-b",
                device_id=device.device_id,
                role=HouseholdRole.OWNER,
                created_at=NOW,
            ),
        )


def test_membership_set_rejects_mismatched_device_or_duplicate_household():
    first = HouseholdMembership(
        household_id="household-a",
        device_id="install-1",
        role="owner",
        created_at=NOW,
    )
    with pytest.raises(ValidationError):
        DeviceMemberships(
            device_id="install-2",
            memberships=(first,),
        )
    with pytest.raises(ValidationError):
        DeviceMemberships(
            device_id="install-1",
            memberships=(first, first),
        )


def test_invite_is_hashed_household_scoped_and_has_consistent_one_use_record():
    invite = HouseholdInviteRecord(
        invite_id="invite-1",
        household_id="household-a",
        token_hash=HASH,
        created_by_device_id="owner-install",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    assert invite.granted_role is HouseholdRole.MEMBER
    redeemed = HouseholdInviteRecord(
        invite_id=invite.invite_id,
        household_id=invite.household_id,
        token_hash=invite.token_hash,
        created_by_device_id=invite.created_by_device_id,
        granted_role=invite.granted_role,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        redeemed_at=NOW + timedelta(minutes=1),
        redeemed_by_device_id="install-2",
    )
    assert redeemed.redeemed_by_device_id == "install-2"

    with pytest.raises(ValidationError):
        HouseholdInviteRecord(
            invite_id="expired-at-creation",
            household_id="household-a",
            token_hash=HASH,
            created_by_device_id="owner-install",
            created_at=NOW,
            expires_at=NOW,
        )
    with pytest.raises(ValidationError):
        HouseholdInviteRecord(
            invite_id="partial-redemption",
            household_id="household-a",
            token_hash=HASH,
            created_by_device_id="owner-install",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
            redeemed_at=NOW + timedelta(minutes=1),
        )
    with pytest.raises(ValidationError):
        HouseholdInviteRecord(
            invite_id="owner-invite",
            household_id="household-a",
            token_hash=HASH,
            created_by_device_id="owner-install",
            granted_role=HouseholdRole.OWNER,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )


def test_recovery_records_are_hash_only_and_scoped_to_device_or_household():
    device_recovery = DeviceRecoveryRecord(
        recovery_id="device-recovery-1",
        device_id="install-1",
        code_hash=HASH,
        created_at=NOW,
    )
    household_recovery = HouseholdRecoveryRecord(
        recovery_id="household-recovery-1",
        household_id="household-a",
        code_hash=HASH,
        created_at=NOW,
    )
    assert device_recovery.device_id == "install-1"
    assert household_recovery.household_id == "household-a"
    with pytest.raises(ValidationError):
        DeviceRecord(
            device_id="install-1",
            credential_hash="plaintext-credential",
            created_at=NOW,
        )
    with pytest.raises(ValidationError):
        DeviceRecord(
            device_id="install-1",
            credential_hash=HASH,
            created_at=NOW,
            password="not-supported",
        )


def test_approval_and_operation_scope_cannot_be_reassigned():
    operation = StoredOperation(
        operation_id="operation-1",
        action="complete_purchase",
        description="Complete purchase",
        values={"item_id": "rice"},
    )
    approval = ApprovalRecord(
        approval_id="approval-1",
        household_id="household-a",
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[operation],
        status=ApprovalStatus.PENDING,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )
    operation_identity = HouseholdOperationIdentity(
        household_id=approval.household_id,
        operation_id=approval.operations[0].operation_id,
    )
    assert operation_identity.household_id == "household-a"

    with pytest.raises(ValidationError):
        approval.household_id = "household-b"
    with pytest.raises(ValidationError):
        operation_identity.household_id = "household-b"


def test_identity_http_request_and_response_envelopes_round_trip():
    context = HouseholdContextHeader.model_validate({"X-Household-Id": "household-a"})
    assert context.requested_household_id == "household-a"
    assert context.model_dump(by_alias=True) == {"X-Household-Id": "household-a"}

    bootstrap_request = BootstrapInstallRequest(bootstrap_challenge="c" * 24)
    bootstrap_response = BootstrapInstallResponse(
        device_id="install-1",
        bearer_credential="b" * 32,
    )
    assert bootstrap_request.bootstrap_challenge == "c" * 24
    assert BootstrapInstallResponse.model_validate(bootstrap_response.model_dump())

    create_household_request = CreateHouseholdRequest()
    create_household_response = CreateHouseholdResponse(household_id="household-a")
    assert create_household_request.model_dump() == {}
    assert CreateHouseholdResponse.model_validate(create_household_response.model_dump())

    list_request = ListHouseholdsRequest()
    list_response = ListHouseholdsResponse(
        households=(HouseholdSummary(household_id="household-a", role=HouseholdRole.OWNER),)
    )
    assert list_request.model_dump() == {}
    assert ListHouseholdsResponse.model_validate(list_response.model_dump())
    assert ListHouseholdsResponse().households == ()

    create_invite_request = CreateInviteRequest()
    create_invite_response = CreateInviteResponse(
        invite_id="invite-1",
        household_id="household-a",
        invite_code="i" * 24,
        expires_at=NOW + timedelta(minutes=10),
    )
    assert create_invite_request.model_dump() == {}
    assert CreateInviteResponse.model_validate(create_invite_response.model_dump())

    redeem_invite_request = RedeemInviteRequest(invite_code="i" * 24)
    redeem_invite_response = RedeemInviteResponse(household_id="household-b")
    assert RedeemInviteRequest.model_validate(redeem_invite_request.model_dump())
    assert RedeemInviteResponse.model_validate(redeem_invite_response.model_dump())

    leave_request = LeaveHouseholdRequest()
    leave_response = LeaveHouseholdResponse(household_id="household-a")
    assert leave_request.model_dump() == {}
    assert LeaveHouseholdResponse.model_validate(leave_response.model_dump())

    role_request = SetMembershipRoleRequest(device_id="install-2", role=HouseholdRole.ADMIN)
    role_response = SetMembershipRoleResponse(
        household_id="household-a",
        device_id="install-2",
        role=HouseholdRole.ADMIN,
    )
    assert SetMembershipRoleRequest.model_validate(role_request.model_dump())
    assert SetMembershipRoleResponse.model_validate(role_response.model_dump())

    revoke_request = RevokeMembershipRequest(device_id="install-2")
    revoke_response = RevokeMembershipResponse(
        household_id="household-a",
        device_id="install-2",
    )
    assert RevokeMembershipRequest.model_validate(revoke_request.model_dump())
    assert RevokeMembershipResponse.model_validate(revoke_response.model_dump())

    issue_device_code_request = IssueDeviceRecoveryCodeRequest()
    issue_device_code_response = IssueDeviceRecoveryCodeResponse(
        device_id="install-1",
        recovery_code="d" * 24,
    )
    assert issue_device_code_request.model_dump() == {}
    assert IssueDeviceRecoveryCodeResponse.model_validate(issue_device_code_response.model_dump())

    redeem_device_request = RedeemDeviceRecoveryRequest(recovery_code="d" * 24)
    redeem_device_response = RedeemDeviceRecoveryResponse(
        revoked_device_id="install-1",
        revoked_membership_count=2,
    )
    assert RedeemDeviceRecoveryRequest.model_validate(redeem_device_request.model_dump())
    assert RedeemDeviceRecoveryResponse.model_validate(redeem_device_response.model_dump())

    issue_household_code_request = IssueHouseholdRecoveryCodeRequest()
    issue_household_code_response = IssueHouseholdRecoveryCodeResponse(
        household_id="household-a",
        recovery_code="h" * 24,
    )
    assert issue_household_code_request.model_dump() == {}
    assert IssueHouseholdRecoveryCodeResponse.model_validate(
        issue_household_code_response.model_dump()
    )

    redeem_household_request = RedeemHouseholdRecoveryRequest(recovery_code="h" * 24)
    redeem_household_response = RedeemHouseholdRecoveryResponse(
        household_id="household-a",
        device_id="replacement-install",
    )
    assert RedeemHouseholdRecoveryRequest.model_validate(redeem_household_request.model_dump())
    assert RedeemHouseholdRecoveryResponse.model_validate(redeem_household_response.model_dump())


def test_http_identity_secrets_are_isolated_from_hash_only_storage_records():
    credential = "bearer-plaintext-secret"
    invite_code = "invite-plaintext-secret"
    recovery_code = "recovery-plaintext-secret"
    issued_credential = BootstrapInstallResponse(
        device_id="install-1",
        bearer_credential=credential,
    )
    issued_invite = CreateInviteResponse(
        invite_id="invite-1",
        household_id="household-a",
        invite_code=invite_code,
        expires_at=NOW + timedelta(minutes=10),
    )
    issued_device_recovery = IssueDeviceRecoveryCodeResponse(
        device_id="install-1",
        recovery_code=recovery_code,
    )
    issued_household_recovery = IssueHouseholdRecoveryCodeResponse(
        household_id="household-a",
        recovery_code=recovery_code,
    )
    assert credential in issued_credential.model_dump_json()
    assert invite_code in issued_invite.model_dump_json()
    assert recovery_code in issued_device_recovery.model_dump_json()
    assert recovery_code in issued_household_recovery.model_dump_json()
    assert credential not in repr(issued_credential)
    assert invite_code not in repr(issued_invite)
    assert recovery_code not in repr(issued_device_recovery)

    stored_records = (
        DeviceRecord(device_id="install-1", credential_hash=HASH, created_at=NOW),
        HouseholdInviteRecord(
            invite_id="invite-1",
            household_id="household-a",
            token_hash=HASH,
            created_by_device_id="install-1",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        ),
        DeviceRecoveryRecord(
            recovery_id="device-recovery-1",
            device_id="install-1",
            code_hash=HASH,
            created_at=NOW,
        ),
        HouseholdRecoveryRecord(
            recovery_id="household-recovery-1",
            household_id="household-a",
            code_hash=HASH,
            created_at=NOW,
        ),
    )
    serialized_records = [record.model_dump_json() for record in stored_records]
    for plaintext_secret in (credential, invite_code, recovery_code):
        assert all(plaintext_secret not in record_json for record_json in serialized_records)
    assert "credential_hash" in DeviceRecord.model_fields
    assert "bearer_credential" not in DeviceRecord.model_fields
    assert "token_hash" in HouseholdInviteRecord.model_fields
    assert "invite_code" not in HouseholdInviteRecord.model_fields
    assert "code_hash" in DeviceRecoveryRecord.model_fields
    assert "recovery_code" not in DeviceRecoveryRecord.model_fields


def test_http_identity_envelopes_reject_invalid_or_untrusted_authorization_fields():
    with pytest.raises(ValidationError):
        HouseholdContextHeader.model_validate({"X-Household-Id": ""})
    with pytest.raises(ValidationError):
        HouseholdContextHeader.model_validate({"X-Household-Id": "household-a", "authorized": True})
    with pytest.raises(ValidationError):
        BootstrapInstallRequest(bootstrap_challenge="short")
    with pytest.raises(ValidationError):
        BootstrapInstallResponse(device_id="install-1", bearer_credential="short")
    with pytest.raises(ValidationError):
        RedeemInviteRequest(invite_code="short")
    with pytest.raises(ValidationError):
        RedeemDeviceRecoveryResponse(
            revoked_device_id="install-1",
            revoked_membership_count=-1,
        )
    with pytest.raises(ValidationError):
        RedeemInviteResponse(household_id="household-a", role=HouseholdRole.OWNER)
    with pytest.raises(ValidationError):
        CreateInviteResponse(
            invite_id="invite-1",
            household_id="household-a",
            invite_code="i" * 24,
            expires_at=NOW + timedelta(minutes=10),
            token_hash=HASH,
        )


def test_household_response_binding_and_operation_scope_remain_household_bound():
    with pytest.raises(ValidationError):
        HouseholdCreation(
            household=HouseholdRecord(
                household_id="household-a",
                created_by_device_id="install-1",
                created_at=NOW,
            ),
            owner_membership=HouseholdMembership(
                household_id="household-b",
                device_id="install-1",
                role=HouseholdRole.OWNER,
                created_at=NOW,
            ),
        )

    operation = HouseholdOperationIdentity(household_id="household-a", operation_id="op-1")
    with pytest.raises(ValidationError):
        operation.operation_id = "op-2"
    with pytest.raises(ValidationError):
        operation.household_id = "household-b"
