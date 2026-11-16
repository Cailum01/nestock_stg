import base64
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.models.commerce import PurchaseOption
from app.services.commerce.demo_data import get_static_demo_options
from app.services.normalizer import ItemNormalizer

logger = logging.getLogger(__name__)


class BigGoCommerceProvider:
    """A2 Commerce Provider Adapter for BigGo Search & Price Comparison.

    Invariants:
    - [D-10 / v8 Section 12] Comparison card shares one operation_id; each option
      has a distinct option_id.
    - [D-10] source explicitly distinguishes 'live' vs 'demo'; demo data must not disguise as live.
    - [INVARIANT-A2-2: ZERO_PRICE_PERSISTENCE] Outputs ephemeral PurchaseOption DTOs;
      never mutates domain inventory or ShoppingItem price.
    - [INVARIANT-A2-3: CANONICAL_NORMALIZATION] Cleans marketing noise and extracts
      standard units via ItemNormalizer.
    - [INVARIANT-A2-4: FAULT_TOLERANT_DEGRADATION] Degrades safely to demo dataset on
      upstream timeout or HTTP errors, avoiding unhandled 500 exceptions (v8 Section 6).
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
        client_id: str | None = None,
        client_secret: str | None = None,
        normalizer: ItemNormalizer | None = None,
        timeout_seconds: float = 10.0,
        demo_mode: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environment is None else environment
        self.demo_mode = demo_mode
        self.client_id = client_id if client_id is not None else env.get("BIGGO_CLIENT_ID")
        self.client_secret = (
            client_secret if client_secret is not None else env.get("BIGGO_CLIENT_SECRET")
        )
        self.normalizer = normalizer or ItemNormalizer()
        self.timeout_seconds = timeout_seconds
        self._cached_token: str | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Factory for async HTTP client, isolated for test patching."""
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    async def _fetch_access_token(
        self, client: httpx.AsyncClient, force_refresh: bool = False
    ) -> str | None:
        """Fetch bearer token from BigGo OAuth2 client_credentials endpoint."""
        if self._cached_token and not force_refresh:
            return self._cached_token

        if not self.client_id or not self.client_secret:
            return None

        credentials = f"{self.client_id}:{self.client_secret}".encode()
        authorization = base64.b64encode(credentials).decode()
        headers = {
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        params = {"grant_type": "client_credentials"}

        response = await client.post(
            "https://api.biggo.com/auth/v1/token",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        token_data = response.json()
        token = str(token_data.get("access_token", "")).strip()
        if token:
            self._cached_token = token
            return token
        return None

    async def search_options(
        self,
        keyword: str,
        limit: int = 5,
    ) -> list[PurchaseOption]:
        """Search products and return normalized PurchaseOption recommendations."""
        clean_keyword = keyword.strip()
        if not clean_keyword:
            return []

        # Demo fallback when demo_mode is True or credentials are not configured
        if self.demo_mode or not self.client_id or not self.client_secret:
            return self._get_demo_fallback_options(clean_keyword, limit)

        encoded_keyword = quote(clean_keyword, safe="")
        search_url = f"https://api.biggo.com/api/v1/spa/search/{encoded_keyword}/product"

        try:
            client = self._get_http_client()
            async with client:
                token = await self._fetch_access_token(client)
                headers = {
                    "Content-Type": "application/json",
                    "site": "biggo.com.tw",
                    "region": "tw",
                }
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                params = {"sort": "price_asc"}
                response = await client.get(search_url, headers=headers, params=params)

                # Token expiration defense: on 401, invalidate cached token and retry once
                if response.status_code == 401 and token:
                    logger.info(
                        "[BIGGO_AUTH] Token rejected (401), refreshing token and retrying..."
                    )
                    self._cached_token = None
                    token = await self._fetch_access_token(client, force_refresh=True)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    else:
                        headers.pop("Authorization", None)
                    response = await client.get(search_url, headers=headers, params=params)

                response.raise_for_status()
                data = response.json()
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning(
                "[BIGGO_DEGRADE] BigGo query failed gracefully, falling back to demo: %s", exc
            )
            return self._get_demo_fallback_options(clean_keyword, limit)
        except Exception as exc:  # Defensive catch-all for parsing or unexpected socket errors
            logger.warning(
                "[BIGGO_DEGRADE] Unexpected error during BigGo retrieval, falling back to demo: %s",
                exc,
            )
            return self._get_demo_fallback_options(clean_keyword, limit)

        # BigGo returns products under 'list' or 'results'
        raw_results = data.get("list") or data.get("results") or []
        return self._normalize_results(raw_results, limit, keyword=clean_keyword)

    def _normalize_results(
        self,
        raw_items: list[dict[str, Any]],
        limit: int,
        keyword: str = "",
    ) -> list[PurchaseOption]:
        """Apply ItemNormalizer to raw provider items to build PurchaseOption DTOs,
        and sort options by estimated_price ascending (cheapest first)."""
        options: list[PurchaseOption] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for item in raw_items:
            raw_title = str(item.get("title", "")).strip()
            if not raw_title:
                continue

            # Bait defense: filter out promotional bait/sample/first-order loss leaders
            if any(bait in raw_title for bait in self.BAIT_KEYWORDS):
                continue

            # Accessory defense: filter out accessories/parts (e.g. 麵粉袋蓋)
            # when searching for core items
            if any(acc in raw_title for acc in self.ACCESSORY_KEYWORDS if acc not in keyword):
                continue

            clean_title = self.normalizer.strip_marketing_noise(raw_title)
            qty, unit, capacity_spec, _ = self.normalizer.resolve_package_and_unit(raw_title)

            # Build unit_spec string
            unit_spec: str | None = None
            if capacity_spec and unit:
                unit_spec = f"{capacity_spec}x{int(qty)}{unit}"
            elif unit:
                unit_spec = f"{int(qty)}{unit}"
            elif capacity_spec:
                unit_spec = capacity_spec

            price_val: float | None = None
            raw_price = item.get("price")
            if raw_price is not None:
                try:
                    clean_price = str(raw_price).replace(",", "").strip()
                    parsed_price = float(clean_price)
                    if parsed_price >= 0.0:
                        price_val = parsed_price
                except (ValueError, TypeError):
                    price_val = None

            # Price floor defense: filter out extreme loss leaders / bait under NT$ 20
            if price_val is not None and price_val < self.MIN_PRICE_FLOOR:
                continue

            # Resolve external product URL (purl, url, or affurl)
            product_url = str(item.get("purl") or item.get("url") or "").strip()
            if not product_url.startswith("http") and item.get("affurl"):
                affurl = str(item["affurl"]).strip()
                product_url = f"https://biggo.com.tw{affurl}" if affurl.startswith("/") else affurl

            if not product_url.startswith("http"):
                continue

            # Store can be a dict ({"name": "酷澎 Coupang"}) or a string
            store_field = item.get("store")
            if isinstance(store_field, dict):
                store_name = str(store_field.get("name") or "BigGo")
            else:
                store_name = str(store_field or item.get("mall_name") or "BigGo")

            options.append(
                PurchaseOption(
                    option_id="opt-biggo-placeholder",
                    title=clean_title or raw_title,
                    raw_title=raw_title,
                    store=store_name,
                    source="live",
                    estimated_price=price_val,
                    product_url=product_url,
                    unit_spec=unit_spec,
                    image_url=item.get("image"),
                    updated_at=now_iso,
                )
            )

        # 比價核心邏輯：依價格由低到高排序 (Price Ascending)，無標價者排在最後
        priced_options = sorted(
            [opt for opt in options if opt.estimated_price is not None],
            key=lambda opt: opt.estimated_price,  # type: ignore[arg-type, return-value]
        )
        unpriced_options = [opt for opt in options if opt.estimated_price is None]
        sorted_options = priced_options + unpriced_options

        final_options = sorted_options[:limit]
        for opt_idx, opt in enumerate(final_options, 1):
            final_options[opt_idx - 1] = opt.model_copy(
                update={"option_id": f"opt-biggo-{opt_idx}"}
            )

        return final_options

    def _get_demo_fallback_options(
        self,
        keyword: str,
        limit: int,
    ) -> list[PurchaseOption]:
        """Provide deterministic demo recommendations matching household scenarios.
        Delegated to shared SSOT demo_data.get_static_demo_options."""
        return get_static_demo_options(keyword, limit=limit)
