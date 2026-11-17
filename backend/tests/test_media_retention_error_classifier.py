"""Unit tests for A2 Media Retention Error Classifier (Refs #98, #176, PR #191)."""

from __future__ import annotations

import re

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseTimeoutError,
)

from app.services.provisional_storage.error_classifier import (
    MediaRetentionErrorClassification,
    classify_media_retention_error,
)

_REGEX = re.compile(r"^[a-z0-9_]{1,64}\Z")


def test_transient_network_timeout() -> None:
    for exc in (ServiceResponseTimeoutError("timeout"), TimeoutError("timed out")):
        res = classify_media_retention_error(exc)
        assert res.is_transient is True
        assert res.error_code == "network_timeout"
        assert res.suggested_backoff_seconds > 0.0
        assert _REGEX.match(res.error_code)


def test_transient_network_connection_error() -> None:
    exceptions = (
        ServiceRequestError("connection failed"),
        ConnectionResetError("reset"),
        ConnectionError("failed"),
    )
    for exc in exceptions:
        res = classify_media_retention_error(exc)
        assert res.is_transient is True
        assert res.error_code == "network_connection_error"
        assert res.suggested_backoff_seconds > 0.0
        assert _REGEX.match(res.error_code)


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_transient_azure_5xx(status_code: int) -> None:
    exc = HttpResponseError(message="Server busy")
    exc.status_code = status_code
    res = classify_media_retention_error(exc)
    assert res.is_transient is True
    assert res.error_code == "service_unavailable"
    assert res.suggested_backoff_seconds > 0.0
    assert _REGEX.match(res.error_code)


def test_transient_azure_429_rate_limited() -> None:
    exc = HttpResponseError(message="Rate limited")
    exc.status_code = 429
    res = classify_media_retention_error(exc)
    assert res.is_transient is True
    assert res.error_code == "rate_limited"
    assert res.suggested_backoff_seconds == 60.0
    assert _REGEX.match(res.error_code)


def test_permanent_blob_not_found() -> None:
    http_404 = HttpResponseError(message="Not found")
    http_404.status_code = 404

    for exc in (ResourceNotFoundError("Blob missing"), FileNotFoundError("file missing"), http_404):
        res = classify_media_retention_error(exc)
        assert res.is_transient is False
        assert res.error_code == "blob_not_found"
        assert _REGEX.match(res.error_code)


def test_permanent_storage_auth_error() -> None:
    http_401 = HttpResponseError(message="Unauthorized")
    http_401.status_code = 401
    http_403 = HttpResponseError(message="Forbidden")
    http_403.status_code = 403

    exceptions = (
        ClientAuthenticationError("Auth failed"),
        PermissionError("denied"),
        http_401,
        http_403,
    )
    for exc in exceptions:
        res = classify_media_retention_error(exc)
        assert res.is_transient is False
        assert res.error_code == "storage_auth_error"
        assert _REGEX.match(res.error_code)


def test_permanent_invalid_reference() -> None:
    for exc in (ValueError("invalid image_ref format"), TypeError("expected str, got None")):
        res = classify_media_retention_error(exc)
        assert res.is_transient is False
        assert res.error_code == "invalid_reference"
        assert _REGEX.match(res.error_code)


def test_permanent_storage_fatal_error_on_unknown_exception() -> None:
    class CustomUnregisteredError(Exception):
        pass

    res = classify_media_retention_error(CustomUnregisteredError("unexpected panic"))
    assert res.is_transient is False
    assert res.error_code == "storage_fatal_error"
    assert _REGEX.match(res.error_code)


def test_error_classification_dataclass_validation() -> None:
    with pytest.raises(ValueError, match="does not match required format"):
        MediaRetentionErrorClassification(
            is_transient=True,
            error_code="INVALID-CODE-WITH-CAPS",
        )
