import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from app.models.commerce import PurchaseOption
from app.services.normalizer import ItemNormalizer

logger = logging.getLogger(__name__)


class GoogleShoppingCommerceProvider:
    """A2 Commerce Backup Provider Adapter for Google Shopping via SerpApi.

    Invariants:
    - [INVARIANT-A2-1: ADAPTER_TRANSPARENCY] Satisfies BaseCommerceProvider interface.
    - [INVARIANT-A2-2: ZERO_PRICE_PERSISTENCE] Outputs ephemeral PurchaseOption DTOs;
      never mutates persistent domain models.
    - [INVARIANT-A2-3: CANONICAL_NORMALIZATION] Normalizes raw titles and extracts unit
      specifications using ItemNormalizer.
    - [INVARIANT-A2-4: FAULT_TOLERANT_DEGRADATION] Catches upstream timeouts and HTTP errors
      gracefully, returning empty list so caller can fallback seamlessly.
    """

    BAIT_KEYWORDS: tuple[str, ...] = (
        "首購",
        "試用",
        "體驗",
        "加價購",
        "樣品",
        "贈品",
        "抽獎",
        "盲盒",
    )
    ACCESSORY_KEYWORDS: tuple[str, ...] = (
        "袋蓋",
        "密封蓋",
        "封口蓋",
        "保鮮蓋",
        "密封夾",
        "封口夾",
        "保鮮夾",
        "分裝袋",
        "分裝盒",
        "收納盒",
        "量勺",
        "量杯",
        "壓頭",
        "噴頭",
    )
    MIN_PRICE_FLOOR: float = 35.0

    def __init__(
        self,
        api_key: str | None = None,
        normalizer: ItemNormalizer | None = None,
        timeout_seconds: float = 5.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environment is None else environment
        self.api_key = (
            api_key
            if api_key is not None
            else env.get("GOOGLE_SHOPPING_API_KEY") or env.get("SERPAPI_API_KEY")
        )
        self.normalizer = normalizer or ItemNormalizer()
        self.timeout_seconds = timeout_seconds

    def _get_http_client(self) -> httpx.AsyncClient:
        """Factory for async HTTP client, isolated for test patching."""
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    @staticmethod
    def _parse_price(raw_price: Any, extracted_price: Any) -> float | None:
        """Extract a valid float price from raw response fields."""
        if isinstance(extracted_price, (int, float)) and extracted_price >= 0:
            return float(extracted_price)

        if isinstance(raw_price, str):
            cleaned = re.sub(r"[^\d.]", "", raw_price)
            try:
                val = float(cleaned)
                if val >= 0:
                    return val
            except (ValueError, TypeError):
                pass
        return None

    async def search_options(
        self,
        keyword: str,
        limit: int = 5,
    ) -> list[PurchaseOption]:
        """Search products on Google Shopping and return normalized PurchaseOptions."""
        clean_keyword = keyword.strip()
        if not clean_keyword:
            return []

        if not self.api_key:
            logger.info("[GSHOP_SKIP] No API key configured for Google Shopping provider")
            return []

        params: dict[str, Any] = {
            "engine": "google_shopping",
            "q": clean_keyword,
            "api_key": self.api_key,
            "gl": "tw",
            "hl": "zh-tw",
        }

        try:
            async with self._get_http_client() as client:
                response = await client.get("https://serpapi.com/search", params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, httpx.TimeoutException, Exception) as exc:
            logger.warning("[GSHOP_ERROR] Google Shopping upstream call failed: %s", exc)
            return []

        shopping_results = payload.get("shopping_results", [])
        if not isinstance(shopping_results, list):
            return []

        now_iso = datetime.now(timezone.utc).isoformat()
        candidates: list[dict[str, Any]] = []

        for item in shopping_results:
            if not isinstance(item, dict):
                continue

            raw_title = str(item.get("title", "")).strip()
            product_url = str(item.get("link") or item.get("product_link", "")).strip()
            if not raw_title or not product_url.startswith("http"):
                continue

            # Bait defense: filter out promotional bait/sample/first-order loss leaders
            if any(bait in raw_title for bait in self.BAIT_KEYWORDS):
                continue

            # Accessory defense: filter out accessories/parts (e.g. 麵粉袋蓋)
            # when searching for core items
            if any(acc in raw_title for acc in self.ACCESSORY_KEYWORDS if acc not in clean_keyword):
                continue

            # ACL: Strip promotional noise and extract package spec
            normalized_title = self.normalizer.strip_marketing_noise(raw_title)
            qty, unit, capacity_spec, _ = self.normalizer.resolve_package_and_unit(raw_title)
            unit_spec: str | None = None
            if capacity_spec and unit:
                unit_spec = f"{capacity_spec}x{int(qty)}{unit}"
            elif unit:
                unit_spec = f"{int(qty)}{unit}"
            elif capacity_spec:
                unit_spec = capacity_spec

            # Store defensive extraction: support dict or string
            store_field = item.get("source") or item.get("merchant")
            if isinstance(store_field, dict):
                store = str(store_field.get("name") or "Google Shopping").strip()
            else:
                store = str(store_field or "Google Shopping").strip()

            price = self._parse_price(item.get("price"), item.get("extracted_price"))

            # Price floor defense: filter out extreme loss leaders / bait under NT$ 20
            if price is not None and price < self.MIN_PRICE_FLOOR:
                continue

            thumbnail = item.get("thumbnail") or item.get("image")

            candidates.append(
                {
                    "title": normalized_title,
                    "raw_title": raw_title,
                    "store": store,
                    "source": "live",
                    "estimated_price": price,
                    "product_url": product_url,
                    "unit_spec": unit_spec,
                    "image_url": str(thumbnail) if thumbnail else None,
                    "updated_at": now_iso,
                }
            )

        # 比價核心邏輯：依價格由低到高排序 (Price Ascending)，無標價者排在最後
        priced = sorted(
            [c for c in candidates if c["estimated_price"] is not None],
            key=lambda c: c["estimated_price"],  # type: ignore[arg-type, return-value]
        )
        unpriced = [c for c in candidates if c["estimated_price"] is None]
        sorted_candidates = (priced + unpriced)[:limit]

        options: list[PurchaseOption] = []
        for idx, c in enumerate(sorted_candidates, 1):
            options.append(
                PurchaseOption(
                    option_id=f"opt-gshop-{idx}",
                    **c,
                )
            )

        return options
