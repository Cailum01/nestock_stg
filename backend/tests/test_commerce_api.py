"""Tests for A2 GET /api/commerce/options endpoint (Issue #139).

Invariants:
- [INVARIANT: NO_PRICE_PERSISTENCE] Zero database write, ephemeral DTOs only.
- [INVARIANT: FIXED_LIMIT_3] Fixed 3 recommendation options, no client limit override.
- [D-10] Options strictly marked with source ('live' vs 'demo'), demo fallback on missing key.
- [TITLE_CONTRACT] Items carry clean title as main display title.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from identity_test_support import authenticated_client

from app.core.dependencies import get_commerce_service
from app.main import app
from app.models.commerce import CommerceSearchStatus, PurchaseOption
from app.services.commerce.biggo import BigGoCommerceProvider


@pytest.fixture
def client() -> TestClient:
    test_client = authenticated_client(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


def test_commerce_options_empty_or_whitespace_keyword(client: TestClient) -> None:
    """Empty or whitespace keyword must return status no_match and empty options."""
    response = client.get("/api/commerce/options", params={"keyword": "   "})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == CommerceSearchStatus.NO_MATCH
    assert data["code"] == "COMMERCE_EMPTY_KEYWORD"
    assert data["message"] == "未提供品名關鍵字"
    assert data["options"] == []

    response_empty = client.get("/api/commerce/options", params={"keyword": ""})
    assert response_empty.status_code == 200
    data_empty = response_empty.json()
    assert data_empty["status"] == CommerceSearchStatus.NO_MATCH
    assert data_empty["code"] == "COMMERCE_EMPTY_KEYWORD"
    assert data_empty["options"] == []


def test_commerce_options_missing_keyword_param(client: TestClient) -> None:
    """Missing keyword query param should raise 422 Unprocessable Entity."""
    response = client.get("/api/commerce/options")
    assert response.status_code == 422


def test_commerce_options_item_not_found(client: TestClient) -> None:
    """When no products match, endpoint returns status no_match and error code."""
    mock_provider = AsyncMock(spec=BigGoCommerceProvider)
    mock_provider.search_options.return_value = []
    app.dependency_overrides[get_commerce_service] = lambda: mock_provider
    try:
        response = client.get("/api/commerce/options", params={"keyword": "non_existent_xyz"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == CommerceSearchStatus.NO_MATCH
        assert data["code"] == "COMMERCE_ITEM_NOT_FOUND"
        assert data["message"] == "查無相符之比價推薦資料"
        assert data["options"] == []
    finally:
        app.dependency_overrides.clear()


def test_commerce_options_demo_fallback_fixed_three(client: TestClient) -> None:
    """In demo mode without external credentials, returns at most 3 demo options."""
    demo_provider = BigGoCommerceProvider(demo_mode=True)
    app.dependency_overrides[get_commerce_service] = lambda: demo_provider
    try:
        response = client.get("/api/commerce/options", params={"keyword": "衛生紙"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == CommerceSearchStatus.SUCCESS
        assert data["code"] == "COMMERCE_OPTIONS_FOUND"
        items = data["options"]
        assert isinstance(items, list)
        assert len(items) <= 3
        assert len(items) > 0

        # Verify sorted by price ascending (cheapest first)
        prices = [item["estimated_price"] for item in items if item["estimated_price"] is not None]
        assert prices == sorted(prices)

        for item in items:
            assert "raw_title" not in item
            assert "option_id" in item
            assert "title" in item
            assert item["title"] != ""
            assert item["source"] in ("live", "demo")
            assert item["source"] == "demo"
            assert "product_url" in item
            assert item["product_url"].startswith("http")
    finally:
        app.dependency_overrides.clear()


def test_commerce_options_ignores_client_limit_tampering(client: TestClient) -> None:
    """Endpoint ignores any attempt by client to specify limit and enforces 3."""
    mock_provider = AsyncMock(spec=BigGoCommerceProvider)
    mock_provider.search_options.return_value = [
        PurchaseOption(
            option_id="opt-1",
            title="商品 1",
            store="momo",
            source="demo",
            estimated_price=100.0,
            product_url="https://example.com/1",
        ),
        PurchaseOption(
            option_id="opt-2",
            title="商品 2",
            store="PChome",
            source="demo",
            estimated_price=120.0,
            product_url="https://example.com/2",
        ),
        PurchaseOption(
            option_id="opt-3",
            title="商品 3",
            store="蝦皮",
            source="demo",
            estimated_price=90.0,
            product_url="https://example.com/3",
        ),
    ]

    app.dependency_overrides[get_commerce_service] = lambda: mock_provider
    try:
        # Client tries to pass limit=10
        response = client.get("/api/commerce/options", params={"keyword": "牛奶", "limit": 10})
        assert response.status_code == 200
        # Verify internal call strictly passed limit=3
        mock_provider.search_options.assert_awaited_once_with("牛奶", limit=3)
        data = response.json()
        assert data["status"] == CommerceSearchStatus.SUCCESS
        assert len(data["options"]) == 3
    finally:
        app.dependency_overrides.clear()


def test_commerce_options_service_unavailable(client: TestClient) -> None:
    """When commerce service raises unhandled exception, endpoint returns status unavailable."""
    mock_provider = AsyncMock(spec=BigGoCommerceProvider)
    mock_provider.search_options.side_effect = RuntimeError("Upstream connection timed out")
    app.dependency_overrides[get_commerce_service] = lambda: mock_provider
    try:
        response = client.get("/api/commerce/options", params={"keyword": "衛生紙"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == CommerceSearchStatus.UNAVAILABLE
        assert data["code"] == "COMMERCE_SEARCH_FAILED"
        assert data["message"] == "外部比價服務暫時無法取得資料"
        assert data["options"] == []
    finally:
        app.dependency_overrides.clear()


def test_commerce_options_requires_install_authentication() -> None:
    """Requests without valid install credentials must be rejected."""
    app.dependency_overrides.clear()
    try:
        with TestClient(app) as anonymous_client:
            response = anonymous_client.get("/api/commerce/options", params={"keyword": "衛生紙"})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()
