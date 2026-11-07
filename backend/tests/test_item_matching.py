"""A1 item matching tests (Issue #36 layers 1 and 2, Issue #44). Read-only fakes."""

import asyncio
from datetime import UTC, datetime

from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus
from app.services.item_matching import ItemMatcher, MatchMethod, match_term, normalize_term

HOUSEHOLD = "demo-household"
OTHER = "another-household"
T = datetime(2026, 1, 1, tzinfo=UTC)


def _item(item_id: str, name: str, *aliases: str, unit: str = "瓶") -> InventoryItem:
    return InventoryItem(
        item_id=item_id,
        household_id=HOUSEHOLD,
        name=name,
        quantity=1,
        unit=unit,
        created_at=T,
        updated_at=T,
        aliases=list(aliases) or None,
    )


def _entry(entry_id: str, name: str, item_id: str | None = None) -> ShoppingItem:
    return ShoppingItem(
        shopping_item_id=entry_id,
        household_id=HOUSEHOLD,
        name=name,
        quantity=1,
        unit="瓶",
        status=ShoppingStatus.PENDING,
        created_at=T,
        updated_at=T,
        item_id=item_id,
    )


SOY = _item("soy", "醬油", "龜甲萬")
MILK = _item("milk", "鮮乳", "牛奶")
BATTERY = _item("battery", "AA電池", unit="顆")
ITEMS = [SOY, MILK, BATTERY]


class FakeInventory:
    def __init__(self, items: list[InventoryItem]) -> None:
        self.items = items
        self.reads = 0

    async def list_items(self, household_id: str) -> list[InventoryItem]:
        self.reads += 1
        return list(self.items) if household_id == HOUSEHOLD else []


class FakeShopping:
    def __init__(self, entries: list[ShoppingItem]) -> None:
        self.entries = entries

    async def list_items(self, household_id: str) -> list[ShoppingItem]:
        return list(self.entries) if household_id == HOUSEHOLD else []


def _matcher(entries: list[ShoppingItem] = ()) -> tuple[ItemMatcher, FakeInventory]:
    inventory = FakeInventory(ITEMS)
    return ItemMatcher(inventory, FakeShopping(list(entries))), inventory


# --- layer 2: exact name or alias -----------------------------------------


def test_exact_name_matches():
    match = match_term("醬油", ITEMS)
    assert match.method == MatchMethod.NAME
    assert match.matched == SOY


def test_alias_matches_when_no_name_does():
    match = match_term("龜甲萬", ITEMS)
    assert match.method == MatchMethod.ALIAS
    assert match.matched == SOY


def test_width_case_and_spaces_are_ignored():
    assert normalize_term(" ＡＡ 電池 ") == normalize_term("aa電池")
    assert match_term("ＡＡ 電池", ITEMS).matched == BATTERY


def test_no_synonym_guessing_here():
    # 「鮮奶」 is not 「鮮乳」 or 「牛奶」 letter for letter: layer 3 (the model) decides.
    match = match_term("鮮奶", ITEMS)
    assert match.method == MatchMethod.NONE
    assert match.matched is None


def test_part_of_a_name_is_not_a_match():
    assert match_term("醬油膏", ITEMS).method == MatchMethod.NONE
    assert match_term("醬", ITEMS).method == MatchMethod.NONE


def test_blank_term_matches_nothing():
    assert match_term("   ", ITEMS).method == MatchMethod.NONE


def test_name_wins_over_another_items_alias():
    rice = _item("rice", "米", unit="包")
    rice_noodle = _item("noodle", "米粉", "米", unit="包")
    match = match_term("米", [rice_noodle, rice])
    assert match.method == MatchMethod.NAME
    assert match.matched == rice


def test_several_items_with_the_same_alias_must_be_chosen():
    a = _item("a", "金蘭醬油", "醬油")
    b = _item("b", "萬字醬油", "醬油")
    match = match_term("醬油", [a, b])
    assert match.method == MatchMethod.ALIAS
    assert match.ambiguous
    assert match.matched is None
    assert match.items == (a, b)


def test_several_terms_read_inventory_once():
    matcher, inventory = _matcher()
    matches = asyncio.run(matcher.match_terms(HOUSEHOLD, ["醬油", "牛奶", "咖啡"]))
    assert [m.method for m in matches] == [MatchMethod.NAME, MatchMethod.ALIAS, MatchMethod.NONE]
    assert inventory.reads == 1


def test_another_household_sees_nothing():
    matcher, _ = _matcher()
    (match,) = asyncio.run(matcher.match_terms(OTHER, ["醬油"]))
    assert match.method == MatchMethod.NONE


# --- layer 1: Shopping List entry with an item_id --------------------------


def test_shopping_entry_with_item_id_uses_it():
    matcher, _ = _matcher([_entry("s1", "隨便寫的名字", item_id="soy")])
    match = asyncio.run(matcher.match_shopping_item(HOUSEHOLD, "s1"))
    assert match.method == MatchMethod.SHOPPING_LIST
    assert match.matched == SOY


def test_shopping_entry_without_item_id_falls_back_to_its_name():
    matcher, _ = _matcher([_entry("s1", "牛奶")])
    match = asyncio.run(matcher.match_shopping_item(HOUSEHOLD, "s1"))
    assert match.method == MatchMethod.ALIAS
    assert match.matched == MILK


def test_shopping_entry_with_unknown_item_id_falls_back_to_its_name():
    matcher, _ = _matcher([_entry("s1", "醬油", item_id="deleted-or-missing")])
    match = asyncio.run(matcher.match_shopping_item(HOUSEHOLD, "s1"))
    assert match.method == MatchMethod.NAME
    assert match.matched == SOY


def test_unknown_or_foreign_shopping_entry_matches_nothing():
    matcher, _ = _matcher([_entry("s1", "醬油", item_id="soy")])
    assert asyncio.run(matcher.match_shopping_item(HOUSEHOLD, "nope")).method == MatchMethod.NONE
    assert asyncio.run(matcher.match_shopping_item(OTHER, "s1")).method == MatchMethod.NONE
