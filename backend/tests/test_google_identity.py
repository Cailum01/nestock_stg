from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.google_identity import (
    GOOGLE_INSTALL_WINDOW,
    GOOGLE_INSTALLS_PER_HOUR,
    GoogleIdentityError,
    GoogleIdentityService,
)


class FakeGoogleInstallRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def provision_google_install(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_google_exchange_issues_install_credential_without_household_scope() -> None:
    repository = FakeGoogleInstallRepository()

    def verify(token: str, audience: str) -> dict[str, Any]:
        assert token == "signed-google-id-token"
        assert audience == "nestock-client"
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "exp": datetime.now(UTC).timestamp() + 60,
            "sub": "google-subject",
        }

    service = GoogleIdentityService(
        repository,
        audience="nestock-client",
        verifier=verify,
    )

    device_id, bearer = asyncio.run(service.authenticate("signed-google-id-token"))

    assert device_id
    assert len(bearer) >= 32
    assert len(repository.calls) == 1
    call = repository.calls[0]
    device = call["device"]
    assert device.device_id == device_id
    assert device.default_household_id is None
    assert device.credential_hash != bearer
    assert call["max_per_hour"] == GOOGLE_INSTALLS_PER_HOUR
    assert call["window"] == GOOGLE_INSTALL_WINDOW == timedelta(hours=1)


def test_google_exchange_fails_closed_before_install_persistence() -> None:
    repository = FakeGoogleInstallRepository()

    def reject(_token: str, _audience: str) -> dict[str, Any]:
        raise ValueError("invalid signature")

    service = GoogleIdentityService(
        repository,
        audience="nestock-client",
        verifier=reject,
    )

    with pytest.raises(GoogleIdentityError):
        asyncio.run(service.authenticate("invalid-token"))

    assert repository.calls == []


def test_google_exchange_fails_closed_when_audience_is_unconfigured() -> None:
    repository = FakeGoogleInstallRepository()
    service = GoogleIdentityService(repository, audience="", verifier=lambda *_: {})

    with pytest.raises(RuntimeError, match="audience is not configured"):
        asyncio.run(service.authenticate("signed-google-id-token"))

    assert repository.calls == []
