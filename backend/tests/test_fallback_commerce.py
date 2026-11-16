"""Unit tests for FallbackCommerceProvider (A2 Cascading Multi-Source Provider).

Verifies:
- Cascading fallback logic: BigGo -> Google Shopping -> Demo Fallback.
- Primary success bypasses secondary and fallback providers.
- Primary failure / empty seamlessly triggers secondary provider.
- Dual upstream failure gracefully degrades to offline demo dataset.
- Zero price persistence and contract adherence.
"""

import pytest

from app.models.commerce import PurchaseOption
from app.services.commerce.base import BaseCommerceProvider
from app.services.commerce.fallback import (
    CommerceProviderUnavailableError,
    FallbackCommerceProvider,
)


def _make_dummy_option(option_id: str, title: str, source: str) -> PurchaseOption:
    return PurchaseOption(
        option_id=option_id,
        title=title,
        store="測試商店",
        source=source,  # type: ignore[arg-type]
        estimated_price=100.0,
        product_url="https://example.com/item",
    )


class DummyProvider(BaseCommerceProvider):
    def __init__(
        self,
        return_options: list[PurchaseOption] | None = None,
        should_fail: bool = False,
    ):
        self.return_options = return_options or []
        self.should_fail = should_fail
        self.call_count = 0

    async def search_options(self, keyword: str, limit: int = 5) -> list[PurchaseOption]:
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError("Upstream service unavailable")
        return self.return_options[:limit]


@pytest.mark.anyio
async def test_fallback_primary_success() -> None:
    """When primary provider succeeds with results, secondary must not be called."""
    primary_opt = _make_dummy_option("opt-biggo-1", "舒潔衛生紙", "live")
    primary = DummyProvider(return_options=[primary_opt])
    secondary = DummyProvider(return_options=[_make_dummy_option("opt-gshop-1", "五月花", "live")])

    fallback = FallbackCommerceProvider(primary_provider=primary, secondary_provider=secondary)
    results = await fallback.search_options("衛生紙", limit=3)

    assert len(results) == 1
    assert results[0].option_id == "opt-biggo-1"
    assert primary.call_count == 1
    assert secondary.call_count == 0


@pytest.mark.anyio
async def test_fallback_secondary_when_primary_fails() -> None:
    """When primary fails with exception, secondary provider must be invoked seamlessly."""
    secondary_opt = _make_dummy_option("opt-gshop-1", "五月花衛生紙", "live")
    primary = DummyProvider(should_fail=True)
    secondary = DummyProvider(return_options=[secondary_opt])

    fallback = FallbackCommerceProvider(primary_provider=primary, secondary_provider=secondary)
    results = await fallback.search_options("衛生紙", limit=3)

    assert len(results) == 1
    assert results[0].option_id == "opt-gshop-1"
    assert primary.call_count == 1
    assert secondary.call_count == 1


@pytest.mark.anyio
async def test_fallback_secondary_when_primary_empty() -> None:
    """When primary returns empty results, secondary provider must be invoked seamlessly."""
    secondary_opt = _make_dummy_option("opt-gshop-1", "五月花衛生紙", "live")
    primary = DummyProvider(return_options=[])
    secondary = DummyProvider(return_options=[secondary_opt])

    fallback = FallbackCommerceProvider(primary_provider=primary, secondary_provider=secondary)
    results = await fallback.search_options("衛生紙", limit=3)

    assert len(results) == 1
    assert results[0].option_id == "opt-gshop-1"
    assert primary.call_count == 1
    assert secondary.call_count == 1


@pytest.mark.anyio
async def test_fallback_demo_when_all_fail() -> None:
    """When both primary and secondary fail, fallback to offline demo dataset with source='demo'."""
    primary = DummyProvider(should_fail=True)
    secondary = DummyProvider(should_fail=True)

    fallback = FallbackCommerceProvider(primary_provider=primary, secondary_provider=secondary)
    results = await fallback.search_options("衛生紙", limit=3)

    assert len(results) > 0
    assert all(opt.source == "demo" for opt in results)
    assert primary.call_count == 1
    assert secondary.call_count == 1


@pytest.mark.anyio
async def test_fallback_empty_keyword() -> None:
    """Empty keyword should immediately return empty list without calling any provider."""
    primary = DummyProvider()
    secondary = DummyProvider()

    fallback = FallbackCommerceProvider(primary_provider=primary, secondary_provider=secondary)
    results = await fallback.search_options("   ")

    assert results == []
    assert primary.call_count == 0
    assert secondary.call_count == 0


@pytest.mark.anyio
async def test_fallback_demo_unmatched_keyword_returns_empty() -> None:
    """When falling back to demo, non-demo keywords must return empty list (no fabricated data)."""
    primary = DummyProvider(should_fail=True)
    secondary = DummyProvider(should_fail=True)

    fallback = FallbackCommerceProvider(primary_provider=primary, secondary_provider=secondary)
    # Search for arbitrary unknown product keyword
    results = await fallback.search_options("不存在的隨機商品_xyz", limit=3)

    # Must be strictly empty list, no fabricated "demo-opt-default (熱銷款)"
    assert results == []
    assert primary.call_count == 1
    assert secondary.call_count == 1


@pytest.mark.anyio
async def test_fallback_raises_unavailable_when_demo_disabled() -> None:
    """When allow_offline_demo is False and upstreams fail, raises unavailable error."""
    primary = DummyProvider(should_fail=True)
    secondary = DummyProvider(should_fail=True)

    fallback = FallbackCommerceProvider(
        primary_provider=primary,
        secondary_provider=secondary,
        allow_offline_demo=False,
    )

    with pytest.raises(CommerceProviderUnavailableError):
        await fallback.search_options("衛生紙", limit=3)


@pytest.mark.anyio
async def test_fallback_demo_covers_manifest_items() -> None:
    """Verify demo fallback properly handles depleted manifest items (flour and detergent)."""
    primary = DummyProvider(should_fail=True)
    secondary = DummyProvider(should_fail=True)

    fallback = FallbackCommerceProvider(primary_provider=primary, secondary_provider=secondary)

    # 1. Flour
    flour_opts = await fallback.search_options("麵粉", limit=3)
    assert len(flour_opts) >= 2
    assert all(opt.source == "demo" for opt in flour_opts)

    # 2. Detergent
    detergent_opts = await fallback.search_options("洗衣精", limit=3)
    assert len(detergent_opts) >= 2
    assert all(opt.source == "demo" for opt in detergent_opts)
