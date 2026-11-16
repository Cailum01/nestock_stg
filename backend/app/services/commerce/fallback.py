import logging
from collections.abc import Mapping

from app.models.commerce import PurchaseOption
from app.services.commerce.base import BaseCommerceProvider
from app.services.commerce.biggo import BigGoCommerceProvider
from app.services.commerce.demo_data import get_static_demo_options
from app.services.commerce.google_shopping import GoogleShoppingCommerceProvider

logger = logging.getLogger(__name__)


class CommerceProviderUnavailableError(Exception):
    """Raised when upstream commerce providers are unavailable and offline demo is disabled."""


def _get_static_demo_options(keyword: str, limit: int = 5) -> list[PurchaseOption]:
    """Provide deterministic offline demo recommendations for known demo items.
    Delegated to shared SSOT demo_data.get_static_demo_options."""
    return get_static_demo_options(keyword, limit=limit)


class FallbackCommerceProvider:
    """A2 Cascading Multi-Source Commerce Provider with automatic graceful degradation.

    Resolution Strategy:
    1. Primary Provider (BigGo): Attempt to query live market price candidates.
    2. Secondary Provider (Google Shopping): If primary fails, throws error, or only yields
       offline demo data, seamlessly query secondary live provider.
    3. Final Fallback (Offline Demo): If all live providers fail and allow_offline_demo is True,
       return deterministic demo recommendations with source='demo'.

    Invariants:
    - [INVARIANT-A2-1: ADAPTER_TRANSPARENCY] Implements BaseCommerceProvider interface.
    - [INVARIANT-A2-2: ZERO_PRICE_PERSISTENCE] Outputs ephemeral display prices only.
    - [INVARIANT-A2-4: FAULT_TOLERANT_DEGRADATION] Zero unhandled exceptions thrown to caller.
    - [INVARIANT-A2-5: DEMO_INTEGRITY] Live data is tagged 'live'; fallback data is tagged 'demo'.
    """

    def __init__(
        self,
        primary_provider: BaseCommerceProvider | None = None,
        secondary_provider: BaseCommerceProvider | None = None,
        environment: Mapping[str, str] | None = None,
        allow_offline_demo: bool = True,
    ) -> None:
        self.primary_provider = (
            primary_provider
            if primary_provider is not None
            else BigGoCommerceProvider(environment=environment)
        )
        self.secondary_provider = (
            secondary_provider
            if secondary_provider is not None
            else GoogleShoppingCommerceProvider(environment=environment)
        )
        self.allow_offline_demo = allow_offline_demo

    async def search_options(
        self,
        keyword: str,
        limit: int = 5,
    ) -> list[PurchaseOption]:
        """Search options with multi-tier cascading fallback."""
        clean_keyword = keyword.strip()
        if not clean_keyword:
            return []

        # Tier 1: Primary provider (BigGo)
        primary_options: list[PurchaseOption] = []
        primary_failed = False
        try:
            primary_options = await self.primary_provider.search_options(clean_keyword, limit=limit)
            # If primary produced live results, return immediately
            if primary_options and any(opt.source == "live" for opt in primary_options):
                return [opt for opt in primary_options if opt.source == "live"][:limit]
        except Exception as exc:
            primary_failed = True
            logger.warning("[FALLBACK_TIER1_FAIL] Primary provider failed: %s", exc)

        # Tier 2: Secondary provider (Google Shopping)
        secondary_failed = False
        try:
            secondary_options = await self.secondary_provider.search_options(
                clean_keyword, limit=limit
            )
            if secondary_options and any(opt.source == "live" for opt in secondary_options):
                return [opt for opt in secondary_options if opt.source == "live"][:limit]
        except Exception as exc:
            secondary_failed = True
            logger.warning("[FALLBACK_TIER2_FAIL] Secondary provider failed: %s", exc)

        # Tier 3: If offline demo is disabled and upstream providers failed, raise error
        if not self.allow_offline_demo and (primary_failed or secondary_failed):
            raise CommerceProviderUnavailableError(
                "All upstream commerce providers failed and offline demo fallback is disabled"
            )

        # If primary already produced demo options, use them; otherwise use static fallback
        if primary_options and any(opt.source == "demo" for opt in primary_options):
            return primary_options[:limit]

        return _get_static_demo_options(clean_keyword, limit=limit)
