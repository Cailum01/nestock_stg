"""B2 reference usage rates, used only when an item has insufficient own records.

Rates are rough per-person daily figures from weak, inconsistent public sources;
they are never shown as facts and never replace the household's own records.
A reference applies only when the item's unit equals the reference unit; items in
瓶/包 etc. do not use it. 洗碗精 (per household, not per person) and 面膜 (usage
varies too widely) are intentionally absent.
"""

from dataclasses import dataclass

from app.models.domain import InventoryItem


@dataclass(frozen=True)
class ReferenceRate:
    keyword: str
    unit: str
    daily_per_person: float


REFERENCE_RATES: tuple[ReferenceRate, ...] = (
    # About 100–200 ml a month; per-wash amounts range from 1.5 to 15 ml.
    ReferenceRate("洗髮精", "ml", 5.0),
    # 2–3 ml per shower (dermatologist guidance) up to 8 ml; daily showers.
    ReferenceRate("沐浴乳", "ml", 4.0),
    # Taiwan toilet-paper study (2008): about 15 g per person per day.
    ReferenceRate("衛生紙", "g", 15.0),
)


def match_reference(
    item: InventoryItem, rates: tuple[ReferenceRate, ...] = REFERENCE_RATES
) -> ReferenceRate | None:
    names = [item.name, *(item.aliases or [])]
    for rate in rates:
        if rate.unit == item.unit and any(rate.keyword in name for name in names):
            return rate
    return None
