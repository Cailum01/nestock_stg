"""Install and household identity endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute

from app.core.dependencies import (
    AuthorizedHousehold,
    get_authenticated_install,
    get_authorized_household,
    get_required_google_identity_service,
    get_required_identity_service,
)
from app.models.identity import (
    BootstrapInstallRequest,
    BootstrapInstallResponse,
    CreateHouseholdRequest,
    CreateHouseholdResponse,
    CreateInviteRequest,
    CreateInviteResponse,
    DeviceRecord,
    GoogleIdentityRequest,
    GoogleIdentityResponse,
    IdentitySessionResponse,
    IssueDeviceRecoveryCodeRequest,
    IssueDeviceRecoveryCodeResponse,
    IssueHouseholdRecoveryCodeRequest,
    IssueHouseholdRecoveryCodeResponse,
    LeaveHouseholdRequest,
    LeaveHouseholdResponse,
    ListHouseholdsResponse,
    RedeemDeviceRecoveryRequest,
    RedeemDeviceRecoveryResponse,
    RedeemHouseholdRecoveryRequest,
    RedeemHouseholdRecoveryResponse,
    RedeemInviteRequest,
    RedeemInviteResponse,
    RevokeMembershipRequest,
    RevokeMembershipResponse,
    SetMembershipRoleRequest,
    SetMembershipRoleResponse,
)
from app.services.google_identity import GoogleIdentityService
from app.services.identity import IdentityError, IdentityService


class IdentityErrorRoute(APIRoute):
    """Translate service identity errors to safe HTTP responses at the API boundary."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def route_handler(request: Request):
            try:
                return await original_handler(request)
            except IdentityError as error:
                raise HTTPException(
                    status_code=error.status_code,
                    detail=error.detail,
                ) from error

        return route_handler


router = APIRouter(tags=["identity"], route_class=IdentityErrorRoute)


def _invite_rate_limit_source(request: Request) -> str:
    """Provide no network identity for the authenticated, per-install invite limit.

    Cloud Run may pass client-controlled forwarding headers through its proxy.
    Rate limiting must not treat those headers, or Uvicorn's derived client,
    as a verified network identity. Invite limiting is additionally keyed by
    the authenticated installation.
    """
    del request
    return "unverified-invite-ingress"


@router.post("/identity/bootstrap", response_model=BootstrapInstallResponse)
async def bootstrap_install(
    request: BootstrapInstallRequest,
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> BootstrapInstallResponse:
    device_id, credential = await service.bootstrap_install(request.bootstrap_challenge)
    return BootstrapInstallResponse(device_id=device_id, bearer_credential=credential)


@router.post("/identity/google", response_model=GoogleIdentityResponse)
async def authenticate_google(
    request: GoogleIdentityRequest,
    service: Annotated[
        GoogleIdentityService,
        Depends(get_required_google_identity_service),
    ],
) -> GoogleIdentityResponse:
    device_id, bearer = await service.authenticate(request.google_id_token)
    return GoogleIdentityResponse(
        device_id=device_id,
        bearer_credential=bearer,
    )


@router.get("/identity/session", response_model=IdentitySessionResponse)
async def get_identity_session(
    device: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> IdentitySessionResponse:
    household_id = device.default_household_id
    if household_id is None:
        households = await service.list_households(device.device_id)
        household_id = households[0].household_id if households else None
    if household_id is None:
        raise HTTPException(
            status_code=409,
            detail="Authenticated install has no default household.",
        )
    await service.authorize_household(
        device_id=device.device_id,
        household_id=household_id,
    )
    return IdentitySessionResponse(household_id=household_id)


@router.post("/households", response_model=CreateHouseholdResponse)
async def create_household(
    request: CreateHouseholdRequest,
    device: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> CreateHouseholdResponse:
    del request
    household_id = await service.create_household(device.device_id)
    return CreateHouseholdResponse(household_id=household_id)


@router.get("/households", response_model=ListHouseholdsResponse)
async def list_households(
    device: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> ListHouseholdsResponse:
    return ListHouseholdsResponse(households=await service.list_households(device.device_id))


@router.post("/households/invites", response_model=CreateInviteResponse)
async def create_invite(
    request: CreateInviteRequest,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> CreateInviteResponse:
    del request
    invite_id, invite_code, expires_at = await service.create_invite(
        actor_device_id=context.device_id,
        household_id=context.household_id,
    )
    return CreateInviteResponse(
        invite_id=invite_id,
        household_id=context.household_id,
        invite_code=invite_code,
        expires_at=expires_at,
    )


@router.post("/households/invites/redeem", response_model=RedeemInviteResponse)
async def redeem_invite(
    request: RedeemInviteRequest,
    http_request: Request,
    device: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> RedeemInviteResponse:
    membership = await service.redeem_invite(
        device_id=device.device_id,
        invite_code=request.invite_code,
        network_source=_invite_rate_limit_source(http_request),
    )
    return RedeemInviteResponse(household_id=membership.household_id)


@router.post("/households/leave", response_model=LeaveHouseholdResponse)
async def leave_household(
    request: LeaveHouseholdRequest,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> LeaveHouseholdResponse:
    del request
    await service.leave_household(
        device_id=context.device_id,
        household_id=context.household_id,
    )
    return LeaveHouseholdResponse(household_id=context.household_id)


@router.put("/households/memberships/role", response_model=SetMembershipRoleResponse)
async def set_membership_role(
    request: SetMembershipRoleRequest,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> SetMembershipRoleResponse:
    membership = await service.set_membership_role(
        actor_device_id=context.device_id,
        household_id=context.household_id,
        target_device_id=request.device_id,
        role=request.role,
    )
    return SetMembershipRoleResponse(
        household_id=context.household_id,
        device_id=request.device_id,
        role=membership.role,
    )


@router.post("/households/memberships/revoke", response_model=RevokeMembershipResponse)
async def revoke_membership(
    request: RevokeMembershipRequest,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> RevokeMembershipResponse:
    await service.revoke_membership(
        actor_device_id=context.device_id,
        household_id=context.household_id,
        target_device_id=request.device_id,
    )
    return RevokeMembershipResponse(
        household_id=context.household_id,
        device_id=request.device_id,
    )


@router.post(
    "/identity/device-recovery-codes",
    response_model=IssueDeviceRecoveryCodeResponse,
)
async def issue_device_recovery_code(
    request: IssueDeviceRecoveryCodeRequest,
    device: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> IssueDeviceRecoveryCodeResponse:
    del request
    code = await service.issue_device_recovery(device.device_id)
    return IssueDeviceRecoveryCodeResponse(device_id=device.device_id, recovery_code=code)


@router.post(
    "/identity/device-recovery/redeem",
    response_model=RedeemDeviceRecoveryResponse,
)
async def redeem_device_recovery(
    request: RedeemDeviceRecoveryRequest,
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> RedeemDeviceRecoveryResponse:
    device_id, membership_count = await service.redeem_device_recovery(request.recovery_code)
    return RedeemDeviceRecoveryResponse(
        revoked_device_id=device_id,
        revoked_membership_count=membership_count,
    )


@router.post(
    "/households/recovery-codes",
    response_model=IssueHouseholdRecoveryCodeResponse,
)
async def issue_household_recovery_code(
    request: IssueHouseholdRecoveryCodeRequest,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> IssueHouseholdRecoveryCodeResponse:
    del request
    code = await service.issue_household_recovery(
        actor_device_id=context.device_id,
        household_id=context.household_id,
    )
    return IssueHouseholdRecoveryCodeResponse(
        household_id=context.household_id,
        recovery_code=code,
    )


@router.post(
    "/households/recovery/redeem",
    response_model=RedeemHouseholdRecoveryResponse,
)
async def redeem_household_recovery(
    request: RedeemHouseholdRecoveryRequest,
    device: Annotated[DeviceRecord, Depends(get_authenticated_install)],
    service: Annotated[IdentityService, Depends(get_required_identity_service)],
) -> RedeemHouseholdRecoveryResponse:
    membership = await service.redeem_household_recovery(
        code=request.recovery_code,
        device_id=device.device_id,
    )
    return RedeemHouseholdRecoveryResponse(
        household_id=membership.household_id,
        device_id=membership.device_id,
    )
