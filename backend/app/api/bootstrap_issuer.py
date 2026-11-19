"""Operator-only bootstrap challenge issuance endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.identity import IdentityErrorRoute
from app.core.dependencies import get_required_bootstrap_issuer_service
from app.models.identity import BootstrapChallengeResponse
from app.services.bootstrap_issuer import BootstrapIssuerService

router = APIRouter(tags=["identity"], route_class=IdentityErrorRoute)
_bearer_auth = HTTPBearer(auto_error=False)


async def _require_operator_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_auth),
    ],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "A Google operator ID token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


@router.post(
    "/identity/bootstrap-challenges",
    response_model=BootstrapChallengeResponse,
)
async def issue_bootstrap_challenge(
    id_token: Annotated[str, Depends(_require_operator_token)],
    service: Annotated[
        BootstrapIssuerService,
        Depends(get_required_bootstrap_issuer_service),
    ],
) -> BootstrapChallengeResponse:
    challenge = await service.issue_challenge(id_token)
    return BootstrapChallengeResponse(bootstrap_challenge=challenge)
