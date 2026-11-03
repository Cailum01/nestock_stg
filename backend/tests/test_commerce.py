"""Unit tests for A2 Commerce Provider and PurchaseOption contract.

Verifies:
- PurchaseOption contract constraints conforming to D-10 (option_id, store, source, updated_at).
- BigGoCommerceProvider demo fallback with source='demo'.
- BigGoCommerceProvider ACL normalization (ItemNormalizer integration) with source='live'.
- BigGoCommerceProvider graceful degradation under timeout and upstream errors.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.models.commerce import PurchaseOption
from app.services.commerce.biggo import BigGoCommerceProvider


def test_purchase_option_model_validation() -> None:
    """Validate field bounds, defaults, and D-10 requirements of PurchaseOption."""
    option = PurchaseOption(
        option_id="opt-test-1",
        title="舒潔 頂級三層抽取衛生紙",
        estimated_price=499.0,
        store="momo購物網",
        source="live",
        product_url="https://example.com/item/123",
        unit_spec="100抽x24包",
        image_url="https://example.com/img.jpg",
    )
    assert option.option_id == "opt-test-1"
    assert option.title == "舒潔 頂級三層抽取衛生紙"
    assert option.estimated_price == 499.0
    assert option.store == "momo購物網"
    assert option.source == "live"
    assert option.product_url == "https://example.com/item/123"
    assert option.unit_spec == "100抽x24包"
    assert option.updated_at is not None

    # D-10 Invariant check: estimated_price can be None (ephemeral)
    free_option = PurchaseOption(
        option_id="opt-demo-free",
        title="贈品試用包",
        store="官方活動",
        source="demo",
        product_url="https://example.com/free",
    )
    assert free_option.estimated_price is None

    # D-10: source must be 'live' or 'demo'
    with pytest.raises(ValidationError):
        PurchaseOption(
            option_id="opt-invalid",
            title="衛生紙",
            store="momo",
            source="unknown",  # invalid literal
            product_url="https://example.com",
        )

    # Invalid cases: missing option_id or empty title/product_url
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


@pytest.mark.anyio
async def test_biggo_provider_demo_fallback() -> None:
    """When credentials are absent, provider returns demo options tagged source='demo'."""
    provider = BigGoCommerceProvider(client_id="", client_secret="")
    results = await provider.search_options("衛生紙", limit=3)

    assert len(results) > 0
    assert any("衛生紙" in opt.title for opt in results)
    for opt in results:
        assert opt.option_id != ""
        assert opt.store != ""
        assert opt.source == "demo"  # D-10: Demo data cannot pretend to be live
        assert opt.product_url.startswith("http")
        assert opt.estimated_price is not None


@pytest.mark.anyio
async def test_biggo_provider_normalizes_raw_marketing_noise() -> None:
    """Verify that marketing tokens in raw BigGo titles are stripped and units extracted."""
    raw_mock_response = {
        "status": 200,
        "list": [
            {
                "oid": "item-momo-123",
                "title": "【限時熱賣爆款】舒潔 頂級三層抽取衛生紙(100抽x24包/箱) [買一送一]",
                "price": 399.0,
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

    # D-10 option_id and store check
    assert option.option_id == "item-momo-123"
    assert option.store == "momo購物網"
    assert option.source == "live"  # D-10: live query tagged live

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
async def test_biggo_provider_timeout_graceful_degradation() -> None:
    """When API request times out, provider returns safe empty list without raising."""
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

    # Graceful degradation invariant: safe fallback empty list
    assert results == []


@pytest.mark.anyio
async def test_biggo_provider_http_error_graceful_degradation() -> None:
    """When upstream returns 503 or HTTP error, provider degrades gracefully."""
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

    assert results == []
