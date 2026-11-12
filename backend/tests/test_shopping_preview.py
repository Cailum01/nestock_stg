from datetime import UTC, datetime

import pytest

from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus
from app.services.shopping.preview import (
    ShoppingDecisionRequired,
    ShoppingPreviewError,
    ShoppingPreviewService,
)

NOW = datetime(2026, 11, 3, tzinfo=UTC)


def items(*, planned=2, stock=3, **shopping_changes):
    shopping = ShoppingItem(
        **{
            "shopping_item_id": "shopping-milk",
            "household_id": "demo-household",
            "item_id": "milk",
            "name": "牛奶",
            "quantity": planned,
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


def preview(shopping, inventory, purchased=1, household="demo-household"):
    return ShoppingPreviewService().preview_purchase(
        household_id=household,
        shopping=shopping,
        inventory=inventory,
        purchased_quantity=purchased,
    )


def test_partial_purchase_completes_task_and_preserves_plan_without_mutation():
    shopping, inventory = items()
    before = (shopping.model_dump(), inventory.model_dump())
    result = preview(shopping, inventory)
    assert result.planned_quantity == 2
    assert result.next_status == ShoppingStatus.COMPLETED
    assert not hasattr(result, "remaining_quantity")
    assert result.inventory_quantity_before == 3
    assert result.inventory_quantity_after == 4
    assert result.purchased_quantity == 1
    assert preview(shopping, inventory) == result
    assert (shopping.model_dump(), inventory.model_dump()) == before


def test_full_purchase_proposes_completed_without_changing_original():
    shopping, inventory = items()
    result = preview(shopping, inventory, purchased=2)
    assert result.planned_quantity == 2
    assert result.next_status == ShoppingStatus.COMPLETED
    assert result.inventory_quantity_after == 5
    assert shopping.status == ShoppingStatus.PENDING


@pytest.mark.parametrize("quantity", [0, -1, 0.2, 1.5, float("nan"), float("inf"), True, "1", None])
def test_invalid_reported_quantities_rejected(quantity):
    with pytest.raises(ShoppingPreviewError):
        preview(*items(), purchased=quantity)


def test_large_integer_counts_do_not_round_through_float():
    shopping, inventory = items()
    # model_copy supplies the #64 integer snapshot without editing the shared model.
    inventory = inventory.model_copy(update={"quantity": 2**60 + 1})
    result = preview(shopping, inventory)
    assert result.inventory_quantity_after == 2**60 + 2
    assert type(result.inventory_quantity_after) is int


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


@pytest.mark.parametrize("planned,stock", [(0, 3), (-1, 3), (2, -1)])
def test_invalid_snapshot_quantity_rejected(planned, stock):
    with pytest.raises(ShoppingPreviewError):
        preview(*items(planned=planned, stock=stock))


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("shopping", "quantity", 0.5),
        ("inventory", "quantity", 0.5),
        ("inventory", "quantity", True),
    ],
)
def test_fractional_or_boolean_snapshot_count_is_not_silently_coerced(target, field, value):
    shopping, inventory = items()
    if target == "shopping":
        shopping = shopping.model_copy(update={field: value})
    else:
        inventory = inventory.model_copy(update={field: value})
    with pytest.raises(ShoppingPreviewError):
        preview(shopping, inventory)


def test_removal_only_describes_pending_entry_without_reversing_purchase():
    shopping, inventory = items(planned=1)
    before = (shopping.model_dump(), inventory.model_dump())
    result = ShoppingPreviewService().preview_removal(
        household_id="demo-household", shopping=shopping
    )
    assert result.shopping_item_id == shopping.shopping_item_id
    assert result.planned_quantity == 1
    assert (shopping.model_dump(), inventory.model_dump()) == before
    assert not hasattr(result, "inventory_quantity_after")


@pytest.mark.parametrize("changes", [{"status": "completed"}, {"household_id": "other"}])
def test_removal_validates_scope_and_state(changes):
    shopping, _ = items(**changes)
    with pytest.raises(ShoppingPreviewError):
        ShoppingPreviewService().preview_removal(household_id="demo-household", shopping=shopping)


@pytest.mark.parametrize("planned,unit", [(None, None), (None, "瓶"), (2, None)])
def test_optional_plan_fields_are_preserved_and_actual_purchase_is_explicit(planned, unit):
    shopping, inventory = items()
    # Transitional snapshot until #64 lands; combined validation also runs on its models.
    shopping = shopping.model_copy(update={"quantity": planned, "unit": unit})
    result = ShoppingPreviewService().preview_purchase(
        household_id="demo-household",
        shopping=shopping,
        inventory=inventory,
        purchased_quantity=1,
        purchased_unit="瓶",
    )
    assert result.planned_quantity == planned
    assert result.unit == "瓶"
    assert result.next_status == ShoppingStatus.COMPLETED
    assert result.inventory_quantity_after == 4
    assert type(result.purchased_quantity) is int
    assert shopping.quantity == planned
    assert shopping.unit == unit
    removed = ShoppingPreviewService().preview_removal(
        household_id="demo-household",
        shopping=shopping,
    )
    assert removed.planned_quantity == planned
    assert removed.unit == unit


def test_name_only_intent_needs_actual_unit_and_never_infers_inventory_unit():
    shopping, inventory = items()
    shopping = shopping.model_copy(update={"quantity": None, "unit": None})
    with pytest.raises(ShoppingDecisionRequired, match="actual purchase unit"):
        preview(shopping, inventory)
    with pytest.raises(ShoppingDecisionRequired, match="canonical units"):
        ShoppingPreviewService().preview_purchase(
            household_id="demo-household",
            shopping=shopping,
            inventory=inventory,
            purchased_quantity=1,
            purchased_unit="箱",
        )


def test_provided_purchase_unit_cannot_override_the_planned_unit():
    shopping, inventory = items()
    inventory = inventory.model_copy(update={"unit": "箱"})
    with pytest.raises(ShoppingDecisionRequired, match="canonical units"):
        ShoppingPreviewService().preview_purchase(
            household_id="demo-household",
            shopping=shopping,
            inventory=inventory,
            purchased_quantity=1,
            purchased_unit="箱",
        )


def test_name_only_intent_uses_explicit_resolved_target_without_guessing_or_creating_link():
    shopping, inventory = items()
    shopping = shopping.model_copy(update={"quantity": None, "unit": None, "item_id": None})
    result = ShoppingPreviewService().preview_purchase(
        household_id="demo-household",
        shopping=shopping,
        inventory=inventory,
        purchased_quantity=3,
        purchased_unit="瓶",
        resolved_inventory_item_id="milk",
    )
    assert result.inventory_quantity_after == 6
    assert result.planned_quantity is None
    assert shopping.item_id is None
    with pytest.raises(ShoppingPreviewError, match="resolved target"):
        ShoppingPreviewService().preview_purchase(
            household_id="demo-household",
            shopping=shopping,
            inventory=inventory,
            purchased_quantity=1,
            purchased_unit="瓶",
            resolved_inventory_item_id="rice",
        )


def test_explicit_resolved_target_cannot_override_an_existing_shopping_link():
    shopping, inventory = items()
    inventory = inventory.model_copy(update={"item_id": "rice"})
    with pytest.raises(ShoppingPreviewError, match="resolved target"):
        ShoppingPreviewService().preview_purchase(
            household_id="demo-household",
            shopping=shopping,
            inventory=inventory,
            purchased_quantity=1,
            purchased_unit="瓶",
            resolved_inventory_item_id="rice",
        )


def test_empty_explicit_target_is_rejected_instead_of_falling_back_to_existing_link():
    shopping, inventory = items()
    with pytest.raises(ShoppingPreviewError, match="must not be empty"):
        ShoppingPreviewService().preview_purchase(
            household_id="demo-household",
            shopping=shopping,
            inventory=inventory,
            purchased_quantity=1,
            resolved_inventory_item_id=" ",
        )
