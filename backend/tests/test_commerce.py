"""Unit tests for A2 Commerce Provider and PurchaseOption contract (D-10 per v8 section 12).

Verifies:
- PurchaseOption contract constraints (option_id, store, source strictly 'live'|'demo').
- BigGoCommerceProvider demo fallback with source='demo' and demo-opt-* IDs.
- BigGoCommerceProvider ACL normalization (ItemNormalizer integration, source='live').
- BigGoCommerceProvider URL percent-encoding for special characters (/, ?).
- BigGoCommerceProvider OAuth2 401 token refresh and single retry.
- BigGoCommerceProvider graceful fallback to demo dataset under timeout and
  upstream errors (v8 Section 6).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.models.commerce import PurchaseOption
from app.services.commerce.biggo import BigGoCommerceProvider


def test_purchase_option_model_validation() -> None:
    """Validate field bounds, defaults, and serializability of PurchaseOption under D-10."""
    option = PurchaseOption(
        option_id="opt-biggo-1",
        title="舒潔 頂級三層抽取衛生紙",
        estimated_price=499.0,
        store="momo購物網",
        source="live",
        product_url="https://example.com/item/123",
        unit_spec="100抽x24包",
        image_url="https://example.com/img.jpg",
        updated_at="2026-11-03T08:00:00Z",
    )
    assert option.option_id == "opt-biggo-1"
    assert option.title == "舒潔 頂級三層抽取衛生紙"
    assert option.store == "momo購物網"
    assert option.source == "live"
    assert option.estimated_price == 499.0
    assert option.product_url == "https://example.com/item/123"
    assert option.unit_spec == "100抽x24包"

    # Invariant check: estimated_price can be None (ephemeral)
    free_option = PurchaseOption(
        option_id="opt-demo-2",
        title="贈品試用包",
        store="官方活動",
        source="demo",
        product_url="https://example.com/free",
    )
    assert free_option.estimated_price is None

    # Invalid cases: missing option_id, empty title, empty product_url, invalid source literal
    with pytest.raises(ValidationError):
        PurchaseOption(
            option_id="",
            title="衛生紙",
            store="momo",
            source="live",
            product_url="https://example.com",
        )

    with pytest.raises(ValidationError):
        PurchaseOption(
            option_id="opt-1",
            title="",
            store="momo",
            source="live",
            product_url="https://example.com",
        )

    with pytest.raises(ValidationError):
        PurchaseOption(
            option_id="opt-1",
            title="衛生紙",
            store="momo",
            source="live",
            product_url="",
        )

    # Invariant check: source must be strictly 'live' or 'demo'
    with pytest.raises(ValidationError):
        PurchaseOption(
            option_id="opt-1",
            title="衛生紙",
            store="momo",
            source="invalid_source",
            product_url="https://example.com",
        )


@pytest.mark.anyio
async def test_biggo_provider_demo_fallback() -> None:
    """When in demo mode or credentials absent, returns demo options (D-10 source='demo')."""
    provider = BigGoCommerceProvider(demo_mode=True)
    results = await provider.search_options("衛生紙", limit=3)

    assert len(results) > 0
    assert any("衛生紙" in opt.title for opt in results)
    for opt in results:
        assert opt.option_id.startswith("demo-opt-")
        assert opt.store != ""
        assert opt.source == "demo"  # Invariant: Demo data must not disguise as live
        assert opt.product_url.startswith("https://biggo.com.tw/s/")
        assert opt.estimated_price is not None
        assert opt.updated_at is not None


@pytest.mark.anyio
async def test_biggo_provider_normalizes_raw_marketing_noise() -> None:
    """Verify that marketing tokens in raw BigGo titles are stripped and units extracted."""
    raw_mock_response = {
        "status": 200,
        "results": [
            {
                "title": "【限時熱賣爆款】舒潔 頂級三層抽取衛生紙(100抽x24包/箱) [買一送一]",
                "price": "399.0",
                "store": {"name": "momo購物網"},
                "url": "https://biggo.com.tw/referral/momo123",
                "image": "https://img.biggo.com/momo123.jpg",
            }
        ],
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = raw_mock_response
    mock_client.get.return_value = mock_resp

    provider = BigGoCommerceProvider(
        client_id="dummy_id",
        client_secret="dummy_secret",
    )

    with (
        patch.object(provider, "_fetch_access_token", return_value="mock_token"),
        patch.object(provider, "_get_http_client", return_value=mock_client),
    ):
        results = await provider.search_options("舒潔衛生紙", limit=5)

    assert len(results) == 1
    option = results[0]

    # D-10 Option structure verification
    assert option.option_id == "opt-biggo-1"
    assert option.store == "momo購物網"
    assert option.source == "live"  # Invariant: Real retrieval must be labeled live

    # Marketing noise removed
    assert "限時熱賣爆款" not in option.title
    assert "買一送一" not in option.title
    assert "【" not in option.title
    assert "舒潔" in option.title

    # Spec extracted
    assert option.estimated_price == 399.0
    assert option.unit_spec is not None
    assert "100抽" in option.unit_spec or "24包" in option.unit_spec


@pytest.mark.anyio
async def test_biggo_provider_encodes_special_characters_in_url() -> None:
    """Verify that special characters (/ and ?) in keyword are percent-encoded in query URL."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": []}
    mock_client.get.return_value = mock_resp

    provider = BigGoCommerceProvider(
        client_id="dummy_id",
        client_secret="dummy_secret",
    )

    with (
        patch.object(provider, "_fetch_access_token", return_value="mock_token"),
        patch.object(provider, "_get_http_client", return_value=mock_client),
    ):
        await provider.search_options("a/b?c=1")

    # Ensure URL called by httpx had / and ? safely quoted as %2F and %3F
    call_url = mock_client.get.call_args[0][0]
    assert "a%2Fb%3Fc%3D1" in call_url
    assert call_url == "https://api.biggo.com/api/v1/spa/search/a%2Fb%3Fc%3D1/product"


@pytest.mark.anyio
async def test_biggo_provider_token_refresh_on_401() -> None:
    """Verify that 401 Unauthorized clears token cache and retries once with a fresh token."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # First call returns 401; second call returns 200
    mock_resp_401 = AsyncMock(spec=httpx.Response)
    mock_resp_401.status_code = 401

    mock_resp_200 = AsyncMock(spec=httpx.Response)
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = {
        "results": [
            {
                "title": "鮮奶",
                "price": 100,
                "store": "家樂福",
                "url": "https://example.com/milk",
            }
        ]
    }

    mock_client.get.side_effect = [mock_resp_401, mock_resp_200]

    provider = BigGoCommerceProvider(
        client_id="dummy_id",
        client_secret="dummy_secret",
    )
    provider._cached_token = "stale_token"

    with (
        patch.object(provider, "_fetch_access_token", AsyncMock(return_value="fresh_token")),
        patch.object(provider, "_get_http_client", return_value=mock_client),
    ):
        results = await provider.search_options("鮮奶")

    assert len(results) == 1
    assert results[0].source == "live"
    assert mock_client.get.call_count == 2


@pytest.mark.anyio
async def test_biggo_provider_timeout_graceful_degradation() -> None:
    """When API request times out, provider returns safe demo options (v8 Section 6)."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.side_effect = httpx.TimeoutException("Request timed out")

    provider = BigGoCommerceProvider(
        client_id="dummy_id",
        client_secret="dummy_secret",
    )

    with (
        patch.object(provider, "_fetch_access_token", return_value="mock_token"),
        patch.object(provider, "_get_http_client", return_value=mock_client),
    ):
        results = await provider.search_options("牛奶")

    # Graceful degradation invariant: fallback demo dataset with source='demo'
    assert len(results) > 0
    assert all(opt.source == "demo" for opt in results)
    assert any("鮮乳" in opt.title or "牛奶" in opt.title for opt in results)


@pytest.mark.anyio
async def test_biggo_provider_http_error_graceful_degradation() -> None:
    """When upstream returns 503 or HTTP error, provider degrades gracefully to demo."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 503
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Service Unavailable", request=AsyncMock(), response=mock_resp
    )
    mock_client.get.return_value = mock_resp

    provider = BigGoCommerceProvider(
        client_id="dummy_id",
        client_secret="dummy_secret",
    )

    with (
        patch.object(provider, "_fetch_access_token", return_value="mock_token"),
        patch.object(provider, "_get_http_client", return_value=mock_client),
    ):
        results = await provider.search_options("醬油")

    assert len(results) > 0
    assert all(opt.source == "demo" for opt in results)
