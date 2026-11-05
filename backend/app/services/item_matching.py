"""A1 item matching (Issue #36 plan B, Issue #44): which household item is this?

Layers implemented here, cheapest first:
1. A Shopping List entry that already carries an ``item_id`` resolves to that item.
2. An exact name or alias match against the household's inventory.

Layer 3 (the Agent's model compares an unmatched name with the household item
list) happens inside the Agent's normal model call; this service only reports
"no match" and hands over the item list. Layer 4 (remember the user's term as an
alias on confirmation) is a write and waits for the approval flow (Issue #48).

This service only reads, through the same household-scoped services as the API
routes. It never writes and never asks A2 to read inventory (Issue #44).
Soft-deleted items are not visible to ``InventoryService.list_items`` yet, so a
term that only matches a deleted item is reported as unmatched for now.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from app.models.domain import InventoryItem
from app.services.interfaces import InventoryService, ShoppingService


class MatchMethod(StrEnum):
    SHOPPING_LIST = "shopping_list"
    NAME = "name"
    ALIAS = "alias"
    NONE = "none"


@dataclass(frozen=True)
class ItemMatch:
    """Result for one term. More than one item means the user must choose."""

    term: str
    method: MatchMethod
    items: tuple[InventoryItem, ...] = ()

    @property
    def matched(self) -> InventoryItem | None:
        return self.items[0] if len(self.items) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.items) > 1


def normalize_term(value: str) -> str:
    """Compare names as typed, ignoring width, letter case and spaces only.

    NFKC folds full-width forms ("ＡＡ" and "AA"); no synonyms or stemming here.
    """
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(folded.split())


def match_term(term: str, items: list[InventoryItem]) -> ItemMatch:
    """Layer 2 on an already-read item list: exact name first, then exact alias."""
    key = normalize_term(term)
    if not key:
        return ItemMatch(term=term, method=MatchMethod.NONE)
    by_name = tuple(item for item in items if normalize_term(item.name) == key)
    if by_name:
        return ItemMatch(term=term, method=MatchMethod.NAME, items=by_name)
    by_alias = tuple(
        item for item in items if any(normalize_term(alias) == key for alias in item.aliases or [])
    )
    if by_alias:
        return ItemMatch(term=term, method=MatchMethod.ALIAS, items=by_alias)
    return ItemMatch(term=term, method=MatchMethod.NONE)


class ItemMatcher:
    """Household-scoped matching over the shared read services."""

    def __init__(self, inventory: InventoryService, shopping: ShoppingService) -> None:
        self._inventory = inventory
        self._shopping = shopping

    async def household_items(self, household_id: str) -> list[InventoryItem]:
        return await self._inventory.list_items(household_id)

    async def match_terms(self, household_id: str, terms: list[str]) -> list[ItemMatch]:
        """Layer 2 for several terms with one inventory read."""
        items = await self.household_items(household_id)
        return [match_term(term, items) for term in terms]

    async def match_shopping_item(self, household_id: str, shopping_item_id: str) -> ItemMatch:
        """Layer 1, falling back to layer 2 on the entry's name.

        An entry from another household, or one that does not exist, matches nothing.
        """
        entries = await self._shopping.list_items(household_id)
        entry = next((e for e in entries if e.shopping_item_id == shopping_item_id), None)
        if entry is None:
            return ItemMatch(term=shopping_item_id, method=MatchMethod.NONE)
        items = await self.household_items(household_id)
        if entry.item_id is not None:
            linked = tuple(item for item in items if item.item_id == entry.item_id)
            if linked:
                return ItemMatch(term=entry.name, method=MatchMethod.SHOPPING_LIST, items=linked)
        return match_term(entry.name, items)
