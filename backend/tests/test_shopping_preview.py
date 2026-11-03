from datetime import UTC, datetime

import pytest

from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus
from app.services.shopping.preview import (
    ShoppingDecisionRequired,
    ShoppingPreviewError,
    ShoppingPreviewService,
)

NOW = datetime(2026, 11, 3, tzinfo=UTC)


def items(*, outstanding=2.0, stock=3.0, **shopping_changes):
    shopping = ShoppingItem(
        **{
            "shopping_item_id": "shopping-milk",
            "household_id": "demo-household",
            "item_id": "milk",
            "name": "牛奶",
            "quantity": outstanding,
            "unit": "瓶",
            "status": "pending",
            "created_at": NOW,
            "updated_at": NOW,
            **shopping_changes,
        }
    )
    inventory = InventoryItem(
        item_id="milk",
        household_id="demo-household",
        name="牛奶",
        quantity=stock,
        unit="瓶",
        created_at=NOW,
        updated_at=NOW,
    )
    return shopping, inventory


def preview(shopping, inventory, purchased=1.0, household="demo-household"):
    return ShoppingPreviewService().preview_purchase(
        household_id=household,
        shopping=shopping,
        inventory=inventory,
        purchased_quantity=purchased,
    )


def test_partial_purchase_and_repeated_preview_never_mutate_snapshots():
    shopping, inventory = items()
    before = (shopping.model_dump(), inventory.model_dump())
    result = preview(shopping, inventory)
    assert result.remaining_quantity == 1
    assert result.next_status == ShoppingStatus.PENDING
    assert result.inventory_quantity_before == 3
    assert result.inventory_quantity_after == 4
    assert result.purchased_quantity == 1
    assert preview(shopping, inventory) == result
    assert (shopping.model_dump(), inventory.model_dump()) == before


def test_full_purchase_proposes_completed_without_changing_original():
    shopping, inventory = items()
    result = preview(shopping, inventory, purchased=2)
    assert result.remaining_quantity == 0
    assert result.next_status == ShoppingStatus.COMPLETED
    assert result.inventory_quantity_after == 5
    assert shopping.status == ShoppingStatus.PENDING


def test_decimal_remainder_can_be_completed_without_float_dust():
    shopping, inventory = items(outstanding=0.3, stock=0)
    first = preview(shopping, inventory, purchased=0.1)
    assert first.remaining_quantity == 0.2
    next_shopping, next_inventory = items(outstanding=first.remaining_quantity, stock=0.1)
    final = preview(next_shopping, next_inventory, purchased=0.2)
    assert final.remaining_quantity == 0
    assert final.next_status == ShoppingStatus.COMPLETED
    assert final.inventory_quantity_after == 0.3


@pytest.mark.parametrize("quantity", [0, -1, float("nan"), float("inf"), True, "1"])
def test_invalid_reported_quantities_rejected(quantity):
    with pytest.raises(ShoppingPreviewError):
        preview(*items(), purchased=quantity)


@pytest.mark.parametrize("changes", [{"status": "completed"}, {"household_id": "other"}])
def test_invalid_shopping_scope_or_state_rejected(changes):
    with pytest.raises(ShoppingPreviewError):
        preview(*items(**changes))


@pytest.mark.parametrize("changes", [{"household_id": "other"}, {"item_id": "rice"}])
def test_wrong_inventory_scope_or_target_rejected(changes):
    shopping, inventory = items()
    with pytest.raises(ShoppingPreviewError):
        preview(shopping, inventory.model_copy(update=changes))


def test_unknown_unit_conversion_is_not_guessed():
    shopping, inventory = items()
    with pytest.raises(ShoppingDecisionRequired):
        preview(shopping, inventory.model_copy(update={"unit": "箱"}))


def test_unlinked_purchase_is_an_explicit_gap_not_an_inferred_match():
    shopping, inventory = items(item_id=None)
    with pytest.raises(ShoppingDecisionRequired):
        preview(shopping, inventory)
    with pytest.raises(ShoppingDecisionRequired):
        preview(*items()[:1], None)


def test_over_purchase_is_deferred_without_clamping_actual_quantity():
    with pytest.raises(ShoppingDecisionRequired):
        preview(*items(), purchased=3)


@pytest.mark.parametrize("outstanding,stock", [(0, 3), (-1, 3), (2, -1)])
def test_invalid_snapshot_quantity_rejected(outstanding, stock):
    with pytest.raises(ShoppingPreviewError):
        preview(*items(outstanding=outstanding, stock=stock))


def test_result_overflow_rejected():
    with pytest.raises(ShoppingPreviewError):
        preview(*items(outstanding=1e308, stock=1e308), purchased=1e308)


def test_removal_only_describes_pending_entry_without_reversing_purchase():
    shopping, inventory = items(outstanding=1)
    before = (shopping.model_dump(), inventory.model_dump())
    result = ShoppingPreviewService().preview_removal(
        household_id="demo-household", shopping=shopping
    )
    assert result.shopping_item_id == shopping.shopping_item_id
    assert result.remaining_quantity == 1
    assert (shopping.model_dump(), inventory.model_dump()) == before
    assert not hasattr(result, "inventory_quantity_after")


@pytest.mark.parametrize("changes", [{"status": "completed"}, {"household_id": "other"}])
def test_removal_validates_scope_and_state(changes):
    shopping, _ = items(**changes)
    with pytest.raises(ShoppingPreviewError):
        ShoppingPreviewService().preview_removal(household_id="demo-household", shopping=shopping)
