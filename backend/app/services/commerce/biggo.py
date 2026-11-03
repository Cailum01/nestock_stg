import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from app.models.commerce import PurchaseOption
from app.services.normalizer import ItemNormalizer

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[3]
load_dotenv(BACKEND_DIR / ".env", override=False)


class BigGoCommerceProvider:
    """A2 Commerce Provider Adapter for BigGo Search & Price Comparison.

    Invariants:
    - [INVARIANT-A2-2: ZERO_PRICE_PERSISTENCE] Outputs ephemeral PurchaseOption DTOs;
      never mutates domain inventory or ShoppingItem price.
    - [INVARIANT-A2-3: CANONICAL_NORMALIZATION] Cleans marketing noise and extracts
      standard units via ItemNormalizer.
    - [INVARIANT-A2-4: FAULT_TOLERANT_DEGRADATION] Degrades safely to empty list on
      upstream timeout or HTTP errors, avoiding unhandled 500 exceptions.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        normalizer: ItemNormalizer | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.client_id = client_id or os.getenv("BIGGO_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("BIGGO_CLIENT_SECRET")
        self.normalizer = normalizer or ItemNormalizer()
        self.timeout_seconds = timeout_seconds
        self._cached_token: str | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Factory for async HTTP client, isolated for test patching."""
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    async def _fetch_access_token(self, client: httpx.AsyncClient) -> str | None:
        """Fetch bearer token from BigGo OAuth2 client_credentials endpoint."""
        if self._cached_token:
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

        # Demo fallback when credentials are not configured
        if not self.client_id or not self.client_secret:
            return self._get_demo_fallback_options(clean_keyword, limit)

        search_url = f"https://api.biggo.com/api/v1/spa/search/{clean_keyword}/product"

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

                response = await client.get(search_url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning("[BIGGO_DEGRADE] BigGo query failed gracefully: %s", exc)
            return []
        except Exception as exc:  # Defensive catch-all for parsing or unexpected socket errors
            logger.warning("[BIGGO_DEGRADE] Unexpected error during BigGo retrieval: %s", exc)
            return []

        # BigGo returns products under 'list' or 'results'
        raw_results = data.get("list") or data.get("results") or []
        return self._normalize_results(raw_results, limit)

    def _normalize_results(
        self,
        raw_items: list[dict[str, Any]],
        limit: int,
    ) -> list[PurchaseOption]:
        """Apply ItemNormalizer to raw provider items to build PurchaseOption DTOs."""
        options: list[PurchaseOption] = []

        for item in raw_items:
            if len(options) >= limit:
                break

            raw_title = str(item.get("title", "")).strip()
            if not raw_title:
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
                    parsed_price = float(raw_price)
                    if parsed_price >= 0.0:
                        price_val = parsed_price
                except (ValueError, TypeError):
                    price_val = None

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
                    title=clean_title or raw_title,
                    raw_title=raw_title,
                    estimated_price=price_val,
                    source=store_name,
                    product_url=product_url,
                    unit_spec=unit_spec,
                    image_url=item.get("image"),
                )
            )

        return options

    def _get_demo_fallback_options(
        self,
        keyword: str,
        limit: int,
    ) -> list[PurchaseOption]:
        """Provide deterministic demo recommendations matching household scenarios."""
        options: list[PurchaseOption] = []

        if "衛生紙" in keyword:
            options.append(
                PurchaseOption(
                    title="舒潔 頂級三層抽取衛生紙",
                    raw_title="舒潔 頂級三層抽取衛生紙 100抽x24包",
                    estimated_price=389.0,
                    source="momo購物網",
                    product_url="https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=demo1",
                    unit_spec="100抽x24包",
                    image_url="https://img.biggo.com.tw/demo_tissue.jpg",
                )
            )
            options.append(
                PurchaseOption(
                    title="五月花 蓬厚柔抽取式衛生紙",
                    raw_title="五月花 蓬厚柔抽取式衛生紙 100抽x24包",
                    estimated_price=339.0,
                    source="PChome 24h",
                    product_url="https://24h.pchome.com.tw/prod/demo2",
                    unit_spec="100抽x24包",
                )
            )
        elif "牛奶" in keyword or "鮮奶" in keyword or "乳" in keyword:
            options.append(
                PurchaseOption(
                    title="光泉 全脂鮮乳",
                    raw_title="光泉 全脂鮮乳 936ml x 2瓶",
                    estimated_price=185.0,
                    source="家樂福線上購物",
                    product_url="https://online.carrefour.com.tw/goods/demo3",
                    unit_spec="936ml x 2瓶",
                )
            )
            options.append(
                PurchaseOption(
                    title="義美 全脂鮮乳",
                    raw_title="義美 全脂鮮乳 1000ml",
                    estimated_price=98.0,
                    source="momo購物網",
                    product_url="https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=demo4",
                    unit_spec="1000ml",
                )
            )
        else:
            options.append(
                PurchaseOption(
                    title=f"精選推薦 {keyword}",
                    raw_title=f"精選特惠 {keyword} 1件裝",
                    estimated_price=199.0,
                    source="momo購物網",
                    product_url=f"https://biggo.com.tw/search/{keyword}",
                    unit_spec="1件",
                )
            )

        return options[:limit]

