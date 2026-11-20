"""Google ID-token verification and app-bearer issuance."""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.models.identity import DeviceRecord
from app.services.identity import IdentityError, hash_secret

logger = logging.getLogger(__name__)
_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
GOOGLE_INSTALLS_PER_HOUR = 3
GOOGLE_INSTALL_WINDOW = timedelta(hours=1)


class GoogleIdentityError(IdentityError):
    """Safe-to-return failure while verifying a Google identity."""

    status_code = 401
    public_detail = "Google identity could not be verified."


class GoogleIdentityRateLimited(IdentityError):
    """Verified Google account has reached its installation issuance limit."""

    status_code = 429
    public_detail = "Google identity installation rate limit exceeded."


class GoogleIdentityRepository(Protocol):
    async def provision_google_install(
        self,
        *,
        subject_hash: str,
        device: DeviceRecord,
        now: datetime,
        max_per_hour: int,
        window: timedelta,
    ) -> None: ...


def verify_google_id_token(token: str, audience: str) -> Mapping[str, Any]:
    """Verify Google's signature and standard claims, then enforce the app audience."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    claims = id_token.verify_oauth2_token(token, Request(), audience=audience)
    if not isinstance(claims, dict):
        raise GoogleIdentityError()
    if claims.get("iss") not in _GOOGLE_ISSUERS or claims.get("aud") != audience:
        raise GoogleIdentityError()
    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise GoogleIdentityError()
    if expiry <= datetime.now(UTC).timestamp():
        raise GoogleIdentityError()
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
        raise GoogleIdentityError()
    return claims


class GoogleIdentityService:
    """Exchange verified Google ID tokens for separate Nestock install bearers."""

    def __init__(
        self,
        repository: GoogleIdentityRepository,
        *,
        audience: str,
        verifier: Callable[[str, str], Mapping[str, Any]] = verify_google_id_token,
    ) -> None:
        self._repository = repository
        self._audience = audience.strip()
        self._verifier = verifier

    async def authenticate(self, google_id_token: str) -> tuple[str, str]:
        if not self._audience:
            raise RuntimeError("Google OAuth audience is not configured.")
        try:
            claims = await asyncio.to_thread(
                self._verifier,
                google_id_token,
                self._audience,
            )
        except GoogleIdentityError:
            raise
        except Exception as error:
            raise GoogleIdentityError() from error

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise GoogleIdentityError()

        now = datetime.now(UTC)
        device_id = uuid.uuid4().hex
        bearer = secrets.token_urlsafe(32)
        device = DeviceRecord(
            device_id=device_id,
            credential_hash=hash_secret(bearer),
            created_at=now,
        )
        await self._repository.provision_google_install(
            subject_hash=hash_secret(subject),
            device=device,
            now=now,
            max_per_hour=GOOGLE_INSTALLS_PER_HOUR,
            window=GOOGLE_INSTALL_WINDOW,
        )
        logger.info("identity.google.install.issued device_id=%s", device_id)
        return device_id, bearer
