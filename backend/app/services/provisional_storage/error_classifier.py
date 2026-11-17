"""A2 媒體保留錯誤分類器 (Media Retention Error Classifier).

依據 Issue #98、Issue #176 與 PR #191 (§8.4) 規範：
提供標準化例外分類，輸出 is_transient、error_code 與建議退避秒數，
供 C1 Retry Worker 判定是否重試 (pending) 或標記人工排查 (needs_attention)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ResourceNotFoundError,
        ServiceRequestError,
        ServiceResponseTimeoutError,
    )
except ImportError:  # pragma: no cover

    class ClientAuthenticationError(Exception):  # type: ignore[no-redef]
        pass

    class HttpResponseError(Exception):  # type: ignore[no-redef]
        status_code: int | None = None

    class ResourceNotFoundError(Exception):  # type: ignore[no-redef]
        pass

    class ServiceRequestError(Exception):  # type: ignore[no-redef]
        pass

    class ServiceResponseTimeoutError(Exception):  # type: ignore[no-redef]
        pass


_ERROR_CODE_REGEX = re.compile(r"^[a-z0-9_]{1,64}\Z")

# 建議退避時間常數 (秒)
DEFAULT_TIMEOUT_BACKOFF_SECONDS = 5.0
DEFAULT_CONNECTION_BACKOFF_SECONDS = 10.0
DEFAULT_SERVICE_UNAVAILABLE_BACKOFF_SECONDS = 30.0
DEFAULT_RATE_LIMITED_BACKOFF_SECONDS = 60.0


@dataclass(frozen=True)
class MediaRetentionErrorClassification:
    """A2 媒體保留錯誤分類結果."""

    is_transient: bool
    error_code: str
    suggested_backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not _ERROR_CODE_REGEX.match(self.error_code):
            raise ValueError(
                f"error_code '{self.error_code}' does not match required format ^[a-z0-9_]{{1,64}}$"
            )


def classify_media_retention_error(
    exc: BaseException,
) -> MediaRetentionErrorClassification:
    """將底層異常分類為暫態 (is_transient=True) 或永久 (is_transient=False).

    :param exc: 捕獲之底層異常
    :return: MediaRetentionErrorClassification
    """
    # 1. 網路/超時 (暫態)
    if isinstance(exc, (ServiceResponseTimeoutError, TimeoutError)):
        return MediaRetentionErrorClassification(
            is_transient=True,
            error_code="network_timeout",
            suggested_backoff_seconds=DEFAULT_TIMEOUT_BACKOFF_SECONDS,
        )

    # 2. 網路連線中斷 / DNS / Socket (暫態)
    if isinstance(exc, (ServiceRequestError, ConnectionError)):
        return MediaRetentionErrorClassification(
            is_transient=True,
            error_code="network_connection_error",
            suggested_backoff_seconds=DEFAULT_CONNECTION_BACKOFF_SECONDS,
        )

    # 3. Azure HTTP 回應錯誤處理
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if status == 429:
            return MediaRetentionErrorClassification(
                is_transient=True,
                error_code="rate_limited",
                suggested_backoff_seconds=DEFAULT_RATE_LIMITED_BACKOFF_SECONDS,
            )
        if status in (500, 502, 503, 504):
            return MediaRetentionErrorClassification(
                is_transient=True,
                error_code="service_unavailable",
                suggested_backoff_seconds=DEFAULT_SERVICE_UNAVAILABLE_BACKOFF_SECONDS,
            )
        if status == 404:
            return MediaRetentionErrorClassification(
                is_transient=False,
                error_code="blob_not_found",
            )
        if status in (401, 403):
            return MediaRetentionErrorClassification(
                is_transient=False,
                error_code="storage_auth_error",
            )

    # 4. 資源不存在 (永久)
    if isinstance(exc, (ResourceNotFoundError, FileNotFoundError)):
        return MediaRetentionErrorClassification(
            is_transient=False,
            error_code="blob_not_found",
        )

    # 5. 授權/驗證失敗 (永久)
    if isinstance(exc, (ClientAuthenticationError, PermissionError)):
        return MediaRetentionErrorClassification(
            is_transient=False,
            error_code="storage_auth_error",
        )

    # 6. 參數或引用格式錯誤 (永久)
    if isinstance(exc, (ValueError, TypeError)):
        return MediaRetentionErrorClassification(
            is_transient=False,
            error_code="invalid_reference",
        )

    # 7. 其餘未列管例外 (永久 - 轉人工排查)
    return MediaRetentionErrorClassification(
        is_transient=False,
        error_code="storage_fatal_error",
    )

