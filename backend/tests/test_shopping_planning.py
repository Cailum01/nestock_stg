from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain import InventoryItem, ShoppingItem, TransactionReason
from app.services.shopping.planning import PurchaseInput, ShoppingPlanService
from app.services.shopping.preview import ShoppingDecisionRequired, ShoppingPreviewError

CREATED = datetime(2026, 11, 3, tzinfo=UTC)
RECORDED = CREATED + timedelta(hours=1)
HOUSEHOLD = "demo-household"


def purchase(key="1", *, item="milk", stock=3.0, actual=1.0, planned=2.0):
    return PurchaseInput(
        operation_id=f"op-{key}",
        transaction_id=f"transaction-{key}",
        shopping=ShoppingItem(
            shopping_item_id=f"shopping-{key}",
            household_id=HOUSEHOLD,
            item_id=item,
            name="牛奶",
            quantity=planned,
            unit="瓶",
            status="pending",
            created_at=CREATED,
            updated_at=CREATED,
        ),
        shopping_etag=f"shopping-etag-{key}",
        inventory=InventoryItem(
            item_id=item,
            household_id=HOUSEHOLD,
            name="牛奶",
            quantity=stock,
            unit="瓶",
            created_at=CREATED,
            updated_at=CREATED,
            location="冰箱",
            aliases=["鮮奶"],
        ),
        inventory_etag=f"inventory-etag-{item}",
        purchased_quantity=actual,
    )


def plan(*purchases, **changes):
    return ShoppingPlanService().plan_purchase(
        **{"household_id": HOUSEHOLD, "purchases": purchases, "recorded_at": RECORDED, **changes}
    )


def test_partial_purchase_builds_actual_transaction_and_preserves_original_plan():
    source = purchase()
    result = plan(source)
    shopping = result.shopping_updates[0]
    inventory = result.inventory_updates[0]
    transaction = result.transactions[0]
    assert shopping.record.quantity == 2
    assert shopping.record.status == "completed"
    assert shopping.record.created_at == CREATED
    assert shopping.record.updated_at == RECORDED
    assert shopping.expected_etag == source.shopping_etag
    assert inventory.record.quantity == 4
    assert inventory.record.location == "冰箱"
    assert inventory.record.aliases == ["鮮奶"]
    assert inventory.expected_etag == source.inventory_etag
    assert inventory.operation_ids == (source.operation_id,)
    assert transaction.reason == TransactionReason.PURCHASE
    assert transaction.quantity_delta == 1
    assert transaction.operation_id == source.operation_id
    assert transaction.transaction_id == source.transaction_id
    assert transaction.created_at == RECORDED
    assert source.shopping.status == "pending"
    assert source.shopping.quantity == 2
    assert source.inventory.quantity == 3
    # Replanning the same stored proposal produces identical identities and values.
    assert plan(source) == result


def test_multiple_shopping_entries_for_one_inventory_produce_one_combined_update():
    first = purchase(stock=0.1, actual=0.2)
    second = purchase("2", stock=0.1, actual=0.3)
    result = plan(first, second)
    assert len(result.shopping_updates) == len(result.transactions) == 2
    assert len(result.inventory_updates) == 1
    assert result.inventory_updates[0].record.quantity == 0.6
    assert result.inventory_updates[0].operation_ids == ("op-1", "op-2")
    assert [transaction.quantity_delta for transaction in result.transactions] == [0.2, 0.3]


def test_only_supplied_selection_is_planned_and_inventory_targets_stay_separate():
    first, second, unselected = purchase(), purchase("2", item="rice"), purchase("3")
    result = plan(first, second)
    assert {row.record.item_id for row in result.inventory_updates} == {"milk", "rice"}
    assert {row.operation_id for row in result.shopping_updates} == {"op-1", "op-2"}
    assert unselected.shopping.status == "pending"


@pytest.mark.parametrize("field", ["operation_id", "transaction_id", "shopping"])
def test_duplicate_identities_are_rejected(field):
    first, second = purchase(), purchase("2")
    with pytest.raises(ShoppingPreviewError, match="Duplicate"):
        plan(first, replace(second, **{field: getattr(first, field)}))


@pytest.mark.parametrize(
    "field", ["operation_id", "transaction_id", "shopping_etag", "inventory_etag"]
)
def test_missing_operation_identity_or_source_version_is_rejected(field):
    with pytest.raises(ShoppingPreviewError, match="non-empty"):
        plan(replace(purchase(), **{field: " "}))


@pytest.mark.parametrize("changed_snapshot", [False, True])
def test_shared_inventory_versions_and_snapshots_cannot_disagree(changed_snapshot):
    first, second = purchase(), purchase("2")
    second = (
        replace(second, inventory=second.inventory.model_copy(update={"quantity": 4.0}))
        if changed_snapshot
        else replace(second, inventory_etag="new-etag")
    )
    with pytest.raises(ShoppingPreviewError, match="snapshots and ETags"):
        plan(first, second)


@pytest.mark.parametrize("target", ["shopping", "inventory"])
def test_cross_household_member_rejects_entire_plan_without_mutation(target):
    first, second = purchase(), purchase("2")
    second = replace(
        second, **{target: getattr(second, target).model_copy(update={"household_id": "other"})}
    )
    before = (first.shopping.model_dump(), first.inventory.model_dump())
    with pytest.raises(ShoppingPreviewError, match="household"):
        plan(first, second)
    assert (first.shopping.model_dump(), first.inventory.model_dump()) == before


def test_group_overflow_is_rejected_even_when_each_individual_preview_is_finite():
    with pytest.raises(ShoppingPreviewError, match="Combined inventory"):
        plan(
            purchase(stock=0, actual=1e308, planned=1e308),
            purchase("2", stock=0, actual=1e308, planned=1e308),
        )


def test_unresolved_workflow_aborts_group_instead_of_returning_partial_result():
    first = purchase()
    with pytest.raises(ShoppingDecisionRequired):
        plan(first, purchase("2", actual=3))
    assert first.shopping.status == "pending"
    assert first.inventory.quantity == 3


@pytest.mark.parametrize("changes", [{"purchases": ()}, {"recorded_at": datetime(2026, 11, 3)}])
def test_empty_selection_and_unbound_timestamp_are_rejected(changes):
    with pytest.raises(ShoppingPreviewError):
        plan(purchase(), **changes)


def removal(source=None, **changes):
    source = source or purchase()
    return ShoppingPlanService().plan_removal(
        **{
            "household_id": HOUSEHOLD,
            "operation_id": source.operation_id,
            "shopping": source.shopping,
            "shopping_etag": source.shopping_etag,
            "recorded_at": RECORDED,
            **changes,
        }
    )


def test_removal_only_plans_storage_tombstone_and_keeps_pending_distinct_from_completed():
    source = purchase()
    result = removal(source)
    assert result.deleted_at == RECORDED
    assert result.expected_etag == source.shopping_etag
    assert result.shopping_item_id == source.shopping.shopping_item_id
    assert result.operation_id == source.operation_id
    assert source.shopping.status == "pending"
    assert "deleted_at" not in source.shopping.model_dump()
    assert not hasattr(result, "transactions")
    assert not hasattr(result, "inventory_updates")
    assert removal(source) == result


@pytest.mark.parametrize(
    "changes",
    [
        {"household_id": "other"},
        {"operation_id": ""},
        {"shopping_etag": ""},
        {"recorded_at": datetime(2026, 11, 3)},
        {"shopping": purchase().shopping.model_copy(update={"status": "completed"})},
    ],
)
def test_removal_rejects_invalid_scope_state_identity_or_version(changes):
    with pytest.raises(ShoppingPreviewError):
        removal(**changes)
