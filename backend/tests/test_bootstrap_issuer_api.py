from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_required_bootstrap_issuer_service
from app.main import create_app
from app.services.bootstrap_issuer import (
    BootstrapIssuerConfigurationError,
    BootstrapIssuerForbidden,
    BootstrapIssuerRateLimited,
    BootstrapIssuerService,
    BootstrapIssuerUnauthorized,
)
from app.services.identity import hash_secret

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
AUDIENCE = "nestock-bootstrap-issuer-test"
OPERATOR_SUBJECT = "google-subject-operator-1"


class IssuerRepository:
    """Narrow test store; intentionally exposes no household operations."""

    def __init__(self) -> None:
        self.challenges: dict[str, dict[str, object]] = {}
        self.issued: dict[str, list[datetime]] = {}
        self.cooldowns: dict[str, datetime] = {}

    async def create_bootstrap_challenge(
        self,
        *,
        operator_key,
        challenge_hash,
        now,
        expires_at,
        issuance_limit,
        issuance_window,
        cooldown,
    ) -> None:
        if self.cooldowns.get(operator_key, datetime.min.replace(tzinfo=UTC)) > now:
            raise BootstrapIssuerRateLimited()
        cutoff = now - issuance_window
        issued = [item for item in self.issued.get(operator_key, []) if item > cutoff]
        if len(issued) >= issuance_limit:
            raise BootstrapIssuerRateLimited()
        issued.append(now)
        self.issued[operator_key] = issued
        if len(issued) >= issuance_limit:
            self.cooldowns[operator_key] = now + cooldown
        self.challenges[challenge_hash] = {
            "id": challenge_hash,
            "record_type": "bootstrap_challenge",
            "created_at": now,
            "expires_at": expires_at,
        }


def _service(
    repository: IssuerRepository,
    *,
    now: list[datetime] | None = None,
    subjects: frozenset[str] = frozenset({OPERATOR_SUBJECT}),
    audience: str | None = AUDIENCE,
    verifier=None,
) -> BootstrapIssuerService:
    clock_values = now if now is not None else [NOW]
    challenges = count(1)
    return BootstrapIssuerService(
        repository,
        audience=audience,
        operator_subjects=subjects,
        clock=lambda: clock_values[0],
        challenge_factory=lambda: f"operator-challenge-value-with-high-entropy-{next(challenges)}",
        token_verifier=verifier or (lambda token, requested_audience: {"sub": OPERATOR_SUBJECT}),
    )


def _client_for(service: BootstrapIssuerService) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_required_bootstrap_issuer_service] = lambda: service
    return TestClient(app)


def test_bootstrap_issuer_requires_bearer_and_fails_closed_for_invalid_token():
    repository = IssuerRepository()
    with _client_for(_service(repository)) as client:
        assert client.post("/identity/bootstrap-challenges").status_code == 401

    def invalid_token(_token: str, _audience: str):
        raise ValueError("invalid signature or audience")

    with _client_for(_service(repository, verifier=invalid_token)) as client:
        response = client.post(
            "/identity/bootstrap-challenges",
            headers={"Authorization": "Bearer invalid-google-token"},
        )
    assert response.status_code == 401
    assert repository.challenges == {}


@pytest.mark.parametrize(
    ("audience", "subjects", "expected_status"),
    [
        (None, frozenset({OPERATOR_SUBJECT}), 503),
        (AUDIENCE, frozenset(), 503),
        (AUDIENCE, frozenset({"another-subject"}), 403),
    ],
)
def test_bootstrap_issuer_requires_configured_audience_and_allowlisted_subject(
    audience, subjects, expected_status
):
    seen_audiences = []

    def verifier(_token: str, requested_audience: str):
        seen_audiences.append(requested_audience)
        return {"sub": OPERATOR_SUBJECT}

    with _client_for(
        _service(
            IssuerRepository(),
            audience=audience,
            subjects=subjects,
            verifier=verifier,
        )
    ) as client:
        response = client.post(
            "/identity/bootstrap-challenges",
            headers={"Authorization": "Bearer google-id-token"},
        )
    assert response.status_code == expected_status
    if audience is None or not subjects:
        assert seen_audiences == []
    else:
        assert seen_audiences == [AUDIENCE]


def test_bootstrap_issuer_returns_hash_only_challenge_and_enforces_cooldown(caplog):
    repository = IssuerRepository()
    now = [NOW]
    service = _service(repository, now=now)
    with _client_for(service) as client:
        responses = [
            client.post(
                "/identity/bootstrap-challenges",
                headers={"Authorization": "Bearer google-id-token"},
            )
            for _ in range(5)
        ]
        limited = client.post(
            "/identity/bootstrap-challenges",
            headers={"Authorization": "Bearer google-id-token"},
        )

    assert [response.status_code for response in responses] == [200] * 5
    assert limited.status_code == 429
    challenge = responses[0].json()["bootstrap_challenge"]
    assert challenge.startswith("operator-challenge-value-with-high-entropy-")
    assert len(repository.challenges) == 5
    stored_hash, stored_document = next(iter(repository.challenges.items()))
    assert stored_hash == hash_secret(challenge)
    assert challenge not in repr(stored_document)
    assert challenge not in caplog.text
    assert isinstance(stored_document["expires_at"], datetime)
    assert stored_document["expires_at"] == NOW + timedelta(minutes=15)
    assert not hasattr(repository, "list_households")

    now[0] += timedelta(minutes=16)
    with _client_for(service) as client:
        after_cooldown = client.post(
            "/identity/bootstrap-challenges",
            headers={"Authorization": "Bearer google-id-token"},
        )
    assert after_cooldown.status_code == 200


def test_bootstrap_issuer_service_surfaces_fail_closed_conditions():
    repository = IssuerRepository()
    with pytest.raises(BootstrapIssuerConfigurationError):
        asyncio.run(
            _service(repository, audience="", subjects=frozenset()).issue_challenge("token")
        )

    with pytest.raises(BootstrapIssuerUnauthorized):
        asyncio.run(
            _service(
                repository,
                verifier=lambda _token, _audience: (_ for _ in ()).throw(ValueError()),
            ).issue_challenge("invalid")
        )

    with pytest.raises(BootstrapIssuerForbidden):
        asyncio.run(
            _service(
                repository,
                verifier=lambda _token, _audience: {"sub": "not-allowed"},
            ).issue_challenge("valid-signature")
        )
