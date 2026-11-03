"""Unit tests for A2 Commerce Provider and PurchaseOption contract.

Verifies:
- PurchaseOption contract constraints.
- BigGoCommerceProvider demo fallback.
- BigGoCommerceProvider ACL normalization (ItemNormalizer integration).
- BigGoCommerceProvider graceful degradation under timeout and upstream errors.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.models.commerce import PurchaseOption
from app.services.commerce.biggo import BigGoCommerceProvider


def test_purchase_option_model_validation() -> None:
    """Validate field bounds, defaults, and serializability of PurchaseOption."""
    option = PurchaseOption(
        title="舒潔 頂級三層抽取衛生紙",
        estimated_price=499.0,
        source="momo購物網",
        product_url="https://example.com/item/123",
        unit_spec="100抽x24包",
        image_url="https://example.com/img.jpg",
    )
    assert option.title == "舒潔 頂級三層抽取衛生紙"
    assert option.estimated_price == 499.0
    assert option.source == "momo購物網"
    assert option.product_url == "https://example.com/item/123"
    assert option.unit_spec == "100抽x24包"

    # Invariant check: estimated_price can be None (ephemeral)
    free_option = PurchaseOption(
        title="贈品試用包",
        source="官方活動",
        product_url="https://example.com/free",
    )
    assert free_option.estimated_price is None

    # Invalid cases: empty title or product_url
    with pytest.raises(ValidationError):
        PurchaseOption(title="", source="momo", product_url="https://example.com")

    with pytest.raises(ValidationError):
        PurchaseOption(title="衛生紙", source="momo", product_url="")


@pytest.mark.anyio
async def test_biggo_provider_demo_fallback() -> None:
    """When credentials are absent, provider should return deterministic demo options."""
    provider = BigGoCommerceProvider(client_id=None, client_secret=None)
    results = await provider.search_options("衛生紙", limit=3)

    assert len(results) > 0
    assert any("衛生紙" in opt.title for opt in results)
    for opt in results:
        assert opt.source != ""
        assert opt.product_url.startswith("http")
        assert opt.estimated_price is not None


@pytest.mark.anyio
async def test_biggo_provider_normalizes_raw_marketing_noise() -> None:
    """Verify that marketing tokens in raw BigGo titles are stripped and units extracted."""
    raw_mock_response = {
        "status": 200,
        "results": [
            {
                "title": "【限時熱賣爆款】舒潔 頂級三層抽取衛生紙(100抽x24包/箱) [買一送一]",
                "price": 399.0,
                "store": "momo購物網",
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

    # Marketing noise removed
    assert "限時熱賣爆款" not in option.title
    assert "買一送一" not in option.title
    assert "【" not in option.title
    assert "舒潔" in option.title

    # Spec extracted
    assert option.estimated_price == 399.0
    assert option.source == "momo購物網"
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

    with patch.object(provider, "_get_http_client", return_value=mock_client):
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
