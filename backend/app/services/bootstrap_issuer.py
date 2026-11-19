"""Operator-only bootstrap challenge issuance over isolated Firestore records."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.services.identity import IdentityError, hash_secret

ISSUANCE_LIMIT = 5
ISSUANCE_WINDOW = timedelta(minutes=15)
ISSUER_COOLDOWN = timedelta(minutes=15)
CHALLENGE_LIFETIME = timedelta(minutes=15)


class BootstrapIssuerRepository(Protocol):
    """Persistence port limited to issuer limits and bootstrap challenges."""

    async def create_bootstrap_challenge(
        self,
        *,
        operator_key: str,
        challenge_hash: str,
        now: datetime,
        expires_at: datetime,
        issuance_limit: int,
        issuance_window: timedelta,
        cooldown: timedelta,
    ) -> None: ...


class BootstrapIssuerError(IdentityError):
    status_code = 400
    public_detail = "Bootstrap challenge issuance was rejected."


class BootstrapIssuerConfigurationError(BootstrapIssuerError):
    status_code = 503
    public_detail = "Bootstrap issuer authentication is not configured."


class BootstrapIssuerUnauthorized(BootstrapIssuerError):
    status_code = 401
    public_detail = "A valid Google operator ID token is required."


class BootstrapIssuerForbidden(BootstrapIssuerError):
    status_code = 403
    public_detail = "The Google subject is not an authorized bootstrap operator."


class BootstrapIssuerRateLimited(BootstrapIssuerError):
    status_code = 429
    public_detail = "Bootstrap challenge issuance is temporarily rate limited."


def verify_google_id_token(token: str, audience: str) -> Mapping[str, Any]:
    """Verify a Google-signed ID token using Google's rotating public certificates."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, Request(), audience)


class BootstrapIssuerService:
    """Validate operator identity and issue a one-time challenge without domain access."""

    def __init__(
        self,
        repository: BootstrapIssuerRepository,
        *,
        audience: str | None,
        operator_subjects: frozenset[str],
        clock: Callable[[], datetime] | None = None,
        challenge_factory: Callable[[], str] | None = None,
        token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._repository = repository
        self._audience = audience.strip() if audience else ""
        self._operator_subjects = frozenset(
            subject.strip() for subject in operator_subjects if subject.strip()
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._challenge_factory = challenge_factory or (lambda: secrets.token_urlsafe(32))
        self._token_verifier = token_verifier or verify_google_id_token

    async def issue_challenge(self, id_token: str) -> str:
        if not self._audience or not self._operator_subjects:
            raise BootstrapIssuerConfigurationError()
        try:
            claims = await asyncio.to_thread(
                self._token_verifier,
                id_token,
                self._audience,
            )
        except Exception as error:
            raise BootstrapIssuerUnauthorized() from error
        if not isinstance(claims, Mapping):
            raise BootstrapIssuerUnauthorized()
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or subject not in self._operator_subjects:
            raise BootstrapIssuerForbidden()

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Bootstrap issuer clock must return a timezone-aware timestamp.")
        now = now.astimezone(UTC)
        challenge = self._challenge_factory()
        challenge_hash = hash_secret(challenge)
        operator_key = hashlib.sha256(f"bootstrap-issuer\0{subject}".encode()).hexdigest()
        try:
            await self._repository.create_bootstrap_challenge(
                operator_key=operator_key,
                challenge_hash=challenge_hash,
                now=now,
                expires_at=now + CHALLENGE_LIFETIME,
                issuance_limit=ISSUANCE_LIMIT,
                issuance_window=ISSUANCE_WINDOW,
                cooldown=ISSUER_COOLDOWN,
            )
        except BootstrapIssuerRateLimited:
            raise
        return challenge


class FirestoreBootstrapIssuerRepository:
    """Atomic Firestore adapter restricted to issuer limits and bootstrap challenges."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create_bootstrap_challenge(
        self,
        *,
        operator_key: str,
        challenge_hash: str,
        now: datetime,
        expires_at: datetime,
        issuance_limit: int,
        issuance_window: timedelta,
        cooldown: timedelta,
    ) -> None:
        import asyncio

        from app.services.firestore_sdk import _run_transaction, _single_snapshot

        rate_ref = self._client.document(f"identity_attempt_limits/{operator_key}")
        challenge_ref = self._client.document(f"bootstrap_challenges/{challenge_hash}")
        result = {"limited": False}
        cutoff = now - issuance_window

        def parse(value: object) -> datetime | None:
            if not isinstance(value, str):
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(UTC)

        def commit(transaction: Any) -> None:
            result["limited"] = False
            rate_snapshot = _single_snapshot(transaction, rate_ref)
            rate_document = (
                dict(rate_snapshot.to_dict() or {})
                if rate_snapshot is not None and rate_snapshot.exists
                else {}
            )
            cooldown_until = parse(rate_document.get("cooldown_until"))
            if cooldown_until is not None and cooldown_until > now:
                latest_issuance = max(
                    (
                        parsed
                        for value in rate_document.get("issued_at", [])
                        if (parsed := parse(value))
                    ),
                    default=now,
                )
                existing_expiry = parse(rate_document.get("expires_at"))
                rate_document["expires_at"] = max(
                    cooldown_until,
                    latest_issuance + issuance_window,
                    existing_expiry or now,
                )
                transaction.set(rate_ref, rate_document)
                result["limited"] = True
                return

            issued = [
                value
                for value in rate_document.get("issued_at", [])
                if isinstance(value, str)
                and (parsed := parse(value)) is not None
                and parsed > cutoff
            ]
            if len(issued) >= issuance_limit:
                latest_issuance = max(
                    (parsed for value in issued if (parsed := parse(value))),
                    default=now,
                )
                cooldown_time = latest_issuance + cooldown
                rate_document["cooldown_until"] = cooldown_time.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z")
                rate_document["expires_at"] = max(
                    latest_issuance + issuance_window,
                    cooldown_time,
                    parse(rate_document.get("expires_at")) or now,
                )
                transaction.set(rate_ref, rate_document)
                result["limited"] = True
                return

            issued.append(now.isoformat(timespec="microseconds").replace("+00:00", "Z"))
            fields: dict[str, object] = {
                "record_type": "bootstrap_issuer_limit",
                "operator_key": operator_key,
                "issued_at": issued,
                "expires_at": max(parse(value) for value in issued if parse(value))
                + issuance_window,
            }
            if len(issued) >= issuance_limit:
                cooldown_time = now + cooldown
                fields["cooldown_until"] = cooldown_time.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                )
                fields["expires_at"] = max(fields["expires_at"], cooldown_time)

            transaction.create(
                challenge_ref,
                {
                    "id": challenge_hash,
                    "record_type": "bootstrap_challenge",
                    "created_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "expires_at": expires_at,
                },
            )
            transaction.set(rate_ref, fields)

        await asyncio.to_thread(_run_transaction, self._client, commit)
        if result["limited"]:
            raise BootstrapIssuerRateLimited()
