import re

from app.models.normalization import NormalizedItem

# Common FMCG brands in Traditional Chinese market
KNOWN_BRANDS = [
    "義美",
    "統一陽光",
    "統一",
    "光泉",
    "舒潔",
    "可口可樂",
    "百事可樂",
    "桂格",
    "雀巢",
    "萬家香",
    "味全",
    "黑松",
    "金車",
    "五月花",
    "春風",
    "得意的一天",
]

# Marketing noise tokens to strip
MARKETING_NOISE_REGEX = re.compile(
    r"【.*?】|\[.*?\]|（.*?）|\(.*?\)|限時|特價|特惠|買一送一|第二件半價|超值|家庭號|分享包|促銷|新上市|推薦|優惠|熱銷|熱賣|爆款",
    re.IGNORECASE,
)

# Capacity/Volume pattern (e.g. 936ml, 100抽, 330ml, 1.5L)
CAPACITY_REGEX = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:ml|g|kg|l|公克|毫升|公升|抽))",
    re.IGNORECASE,
)

# Multi-piece spec pattern (e.g. 100抽x24包, 6入x4組)
MULTI_PACK_REGEX = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:抽|入|片|張)?\s*[xX*]\s*(\d+)\s*(入|瓶|罐|包|盒|袋|條|支|組|個|箱)",
)

# Single-piece quantity pattern (e.g. 2瓶裝, 6入組, 12罐)
SINGLE_PACK_REGEX = re.compile(
    r"(\d+)\s*(入|瓶|罐|包|盒|袋|條|支|組|個|箱)(?:裝|組)?(?:/[箱盒組袋])?",
)


class ItemNormalizer:
    """Core domain normalizer for product naming, feature keywords, and package units.

    [INVARIANT: A2_NORMALIZATION_SSOT]
    All feature keywords and packaging units must be normalized before persistence.
    """

    def strip_marketing_noise(self, text: str) -> str:
        """Strip brackets, tags, and promotional marketing words."""
        cleaned = MARKETING_NOISE_REGEX.sub(" ", text)
        return re.sub(r"\s+", " ", cleaned).strip()

    def resolve_package_and_unit(
        self, text: str, default_quantity: int = 1, default_unit: str | None = None
    ) -> tuple[int, str | None, str | None, str]:
        """Extract integer quantity, canonical unit, capacity specification, and remaining text."""
        capacity_spec: str | None = None
        quantity: int = max(1, int(default_quantity))
        unit: str | None = default_unit
        working_text = text

        # 1. Check for multi-pack (e.g. 100抽x24包/箱)
        multi_match = MULTI_PACK_REGEX.search(working_text)
        if multi_match:
            sub_count, main_count, matched_unit = multi_match.groups()
            quantity = max(1, int(main_count))
            unit = matched_unit
            # If sub_count is followed by unit like 抽/入
            capacity_match = CAPACITY_REGEX.search(working_text[: multi_match.start() + 10])
            if capacity_match:
                capacity_spec = capacity_match.group(1).strip()
            else:
                capacity_spec = f"{sub_count}抽" if "抽" in working_text else f"{sub_count}入"
            working_text = (
                working_text[: multi_match.start()] + " " + working_text[multi_match.end() :]
            )

        # 2. Check for single pack if not multi-pack
        if not multi_match:
            single_match = SINGLE_PACK_REGEX.search(working_text)
            if single_match:
                qty_str, matched_unit = single_match.groups()
                quantity = max(1, int(qty_str))
                unit = matched_unit
                working_text = (
                    working_text[: single_match.start()] + " " + working_text[single_match.end() :]
                )

        # 3. Extract capacity if not already extracted
        if not capacity_spec:
            cap_match = CAPACITY_REGEX.search(working_text)
            if cap_match:
                capacity_spec = cap_match.group(1).strip()
                working_text = (
                    working_text[: cap_match.start()] + " " + working_text[cap_match.end() :]
                )

        # Remove trailing slashes and clean whitespace
        working_text = re.sub(r"[/\\|]+", " ", working_text)
        cleaned_text = re.sub(r"\s+", " ", working_text).strip()
        return quantity, unit, capacity_spec, cleaned_text

    def extract_brand_and_canonical(self, text: str) -> tuple[str | None, str]:
        """Extract known brand prefix and return canonical product name."""
        working_text = text
        detected_brand: str | None = None

        for brand in KNOWN_BRANDS:
            if working_text.startswith(brand):
                detected_brand = "統一" if brand == "統一陽光" else brand
                working_text = working_text[len(brand) :].strip()
                break

        canonical = working_text.strip() if working_text.strip() else text.strip()
        return detected_brand, canonical

    def extract_feature_keywords(self, canonical_name: str, brand: str | None = None) -> list[str]:
        """Return empty list as feature_keywords is retired under Issue #36 (Plan B).

        Per Issue #36 and Issue #35, item matching uses full `canonical_name` and `aliases`
        instead of static 21-token substring splitting.
        """
        del canonical_name, brand
        return []

    def generate_aliases(self, canonical_name: str, brand: str | None) -> list[str]:
        """Generate brand-prefixed expression without static synonym tables (Issue #36 Plan B)."""
        if brand and canonical_name and not canonical_name.startswith(brand):
            return [f"{brand}{canonical_name}"]
        return []

    def normalize(
        self, raw_text: str, fallback_quantity: int = 1, fallback_unit: str | None = None
    ) -> NormalizedItem:
        """Full pipeline: clean marketing noise -> resolve units -> extract brand & features."""
        if not raw_text or not raw_text.strip():
            raise ValueError("raw_text must be non-empty.")

        # 1. Strip marketing noise
        step1_cleaned = self.strip_marketing_noise(raw_text)

        # 2. Extract package and units
        quantity, unit, capacity, step2_cleaned = self.resolve_package_and_unit(
            step1_cleaned, default_quantity=fallback_quantity, default_unit=fallback_unit
        )

        # 3. Extract brand and core canonical name
        brand, canonical = self.extract_brand_and_canonical(step2_cleaned)

        # 4. Extract feature keywords snapshot (brand isolated)
        features = self.extract_feature_keywords(canonical, brand)

        # 5. Generate search aliases
        aliases = self.generate_aliases(canonical, brand)

        unit_confirmed = unit is not None

        return NormalizedItem(
            raw_text=raw_text.strip(),
            canonical_name=canonical,
            brand=brand,
            feature_keywords=features,
            aliases=aliases,
            quantity=quantity,
            unit=unit,
            unit_confirmed=unit_confirmed,
            capacity_spec=capacity,
        )
