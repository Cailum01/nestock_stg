"""Unit tests for GoogleShoppingCommerceProvider (A2 Commerce Backup Provider).

Verifies:
- GoogleShoppingCommerceProvider initialization and API key handling.
- Correct parsing of Google Shopping / SerpApi response payloads into PurchaseOption.
- ItemNormalizer integration for noise removal and unit specification extraction.
- Graceful handling of network timeouts, HTTP errors, and missing credentials.
- Invariant: estimated_price is ephemeral and never persisted.
"""

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from dotenv import load_dotenv

from app.models.commerce import PurchaseOption
from app.services.commerce.google_shopping import GoogleShoppingCommerceProvider


@pytest.mark.anyio
async def test_google_shopping_search_success_with_normalization() -> None:
    """Validate successful search, SerpApi parsing, and ItemNormalizer title cleaning."""
    mock_payload = {
        "shopping_results": [
            {
                "position": 1,
                "title": "【限時特賣 現貨免運】舒潔 頂級三層抽取式衛生紙 100抽x24包/箱",
                "price": "$499",
                "extracted_price": 499.0,
                "source": "momo購物網",
                "link": "https://www.momo.com.tw/goods/GoodsDetail.jsp?osm=test&m_code=123",
                "thumbnail": "https://img.momo.com.tw/123.jpg",
            },
            {
                "position": 2,
                "title": "五月花 頂級柔韌三層抽取衛生紙 110抽x24包",
                "extracted_price": 450.0,
                "source": "PChome 24h購物",
                "link": "https://24h.pchome.com.tw/prod/DABC123",
            },
        ]
    }

    provider = GoogleShoppingCommerceProvider(api_key="test-gshop-key")

    mock_response = httpx.Response(
        status_code=200,
        json=mock_payload,
        request=httpx.Request("GET", "https://serpapi.com/search"),
    )

    with patch.object(provider, "_get_http_client") as mock_client_factory:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_factory.return_value = mock_client

        options = await provider.search_options("衛生紙", limit=2)

        # Verify query params do not force sort_by (relevance-first strategy)
        called_params = mock_client.get.call_args[1]["params"]
        assert "sort_by" not in called_params

    assert len(options) == 2
    assert all(isinstance(opt, PurchaseOption) for opt in options)

    # Invariant: sorted by price ascending (450.0 < 499.0)
    first = options[0]
    assert first.option_id == "opt-gshop-1"
    assert first.store == "PChome 24h購物"
    assert first.source == "live"
    assert first.estimated_price == 450.0

    second = options[1]
    assert second.option_id == "opt-gshop-2"
    assert second.store == "momo購物網"
    assert second.source == "live"
    assert second.estimated_price == 499.0
    assert second.product_url.startswith("https://www.momo.com.tw")
    assert second.unit_spec is not None
    # Verify ItemNormalizer cleaned promotional noise
    assert "限時特賣" not in second.title
    assert "現貨免運" not in second.title


@pytest.mark.anyio
async def test_google_shopping_empty_keyword() -> None:
    """Empty or whitespace keyword should return empty list without HTTP call."""
    provider = GoogleShoppingCommerceProvider(api_key="test-key")
    options = await provider.search_options("   ")
    assert options == []


@pytest.mark.anyio
async def test_google_shopping_missing_key_returns_empty() -> None:
    """Provider without API key should safely return empty list instead of crashing."""
    provider = GoogleShoppingCommerceProvider(api_key=None, environment={})
    options = await provider.search_options("衛生紙")
    assert options == []


@pytest.mark.anyio
async def test_google_shopping_http_error_handling() -> None:
    """HTTP 500 or timeout should be caught and return empty list."""
    provider = GoogleShoppingCommerceProvider(api_key="test-key")

    with patch.object(provider, "_get_http_client") as mock_client_factory:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = httpx.TimeoutException("Connection timed out")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_factory.return_value = mock_client

        options = await provider.search_options("衛生紙")

    assert options == []


@pytest.mark.anyio
async def test_google_shopping_defensive_url_and_store_extraction() -> None:
    """Validate defensive filtering of non-http URLs and dict store name unpacking."""
    mock_payload = {
        "shopping_results": [
            {
                "title": "無效連結商品1",
                "extracted_price": 100.0,
                "source": "momo",
                "link": "javascript:void(0)",
            },
            {
                "title": "無效連結商品2 (相對路徑)",
                "extracted_price": 120.0,
                "source": "PChome",
                "link": "/prod/12345",
            },
            {
                "title": "有效連結但商店為 dict",
                "extracted_price": 150.0,
                "source": {"name": "家樂福線上購物", "icon": "https://carrefour.com/icon.png"},
                "link": "https://carrefour.com.tw/p/123",
            },
        ]
    }

    provider = GoogleShoppingCommerceProvider(api_key="test-key")
    mock_response = httpx.Response(
        status_code=200,
        json=mock_payload,
        request=httpx.Request("GET", "https://serpapi.com/search"),
    )

    with patch.object(provider, "_get_http_client") as mock_client_factory:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_factory.return_value = mock_client

        options = await provider.search_options("商品", limit=5)

    # Only the valid http link must survive the defensive filter
    assert len(options) == 1
    assert options[0].product_url == "https://carrefour.com.tw/p/123"
    assert options[0].store == "家樂福線上購物"
    assert options[0].option_id == "opt-gshop-1"


@pytest.mark.anyio
async def test_google_shopping_bait_keywords_filtering() -> None:
    """Validate filtering of loss leader bait, samples, and first-order promotions."""
    mock_payload = {
        "shopping_results": [
            {
                "title": "[首購限定 1元] 濕式衛生紙 試用包 10抽",
                "extracted_price": 1.0,
                "source": "酷澎",
                "link": "https://coupang.com/p/1",
            },
            {
                "title": "加價購商品 體驗款 衛生紙",
                "extracted_price": 5.0,
                "source": "蝦皮",
                "link": "https://shopee.tw/p/2",
            },
            {
                "title": "無關鍵字但價格僅8元之引流款",
                "extracted_price": 8.0,
                "source": "酷澎",
                "link": "https://coupang.com/p/8",
            },
            {
                "title": "舒潔 頂級三層抽取式衛生紙 100抽x24包",
                "extracted_price": 389.0,
                "source": "momo",
                "link": "https://momo.com.tw/p/3",
            },
        ]
    }

    provider = GoogleShoppingCommerceProvider(api_key="test-key")
    mock_response = httpx.Response(
        status_code=200,
        json=mock_payload,
        request=httpx.Request("GET", "https://serpapi.com/search"),
    )

    with patch.object(provider, "_get_http_client") as mock_client_factory:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_factory.return_value = mock_client

        options = await provider.search_options("衛生紙", limit=3)

    # 1元首購試用包與加價購體驗款必須被過濾，僅保留 389 元之正常正品
    assert len(options) == 1
    assert options[0].estimated_price == 389.0
    assert options[0].store == "momo"
    assert "首購" not in options[0].title


@pytest.mark.anyio
async def test_google_shopping_filters_accessory_keywords() -> None:
    """Validate accessory keywords like '麵粉袋蓋' are filtered out when searching for flour."""
    mock_payload = {
        "shopping_results": [
            {
                "title": "食品級 麵粉袋密封蓋 / 麵粉袋蓋 2入",
                "extracted_price": 28.0,
                "source": "蝦皮購物",
                "link": "https://shopee.tw/p/lid28",
            },
            {
                "title": "日正 中筋麵粉 1kg",
                "extracted_price": 55.0,
                "source": "全聯線上購",
                "link": "https://pxmart.com.tw/p/flour55",
            },
        ]
    }

    provider = GoogleShoppingCommerceProvider(api_key="test-key")
    mock_response = httpx.Response(
        status_code=200,
        json=mock_payload,
        request=httpx.Request("GET", "https://serpapi.com/search"),
    )

    with patch.object(provider, "_get_http_client") as mock_client_factory:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_factory.return_value = mock_client

        options = await provider.search_options("麵粉", limit=3)

    assert len(options) == 1
    assert options[0].title == "日正 中筋麵粉 1kg"
    assert options[0].estimated_price == 55.0


@pytest.mark.anyio
async def test_google_shopping_live_connectivity() -> None:
    """When SERPAPI_API_KEY is configured in backend/.env, verify real external connectivity."""
    load_dotenv(override=True)
    key = os.getenv("SERPAPI_API_KEY") or os.getenv("GOOGLE_SHOPPING_API_KEY")
    if not key or not key.strip():
        pytest.skip("No SERPAPI_API_KEY configured for live test")

    provider = GoogleShoppingCommerceProvider()
    options = await provider.search_options("洗髮精", limit=2)
    assert len(options) > 0
    assert all(opt.source == "live" for opt in options)
    assert all(opt.estimated_price is not None for opt in options)
