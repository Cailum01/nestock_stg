from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain import InventoryItem, ShoppingItem, TransactionReason
from app.services.shopping.planning import PurchaseInput, ShoppingPlanService
from app.services.shopping.preview import ShoppingDecisionRequired, ShoppingPreviewError

CREATED = datetime(2026, 11, 3, tzinfo=UTC)
RECORDED = CREATED + timedelta(hours=1)
HOUSEHOLD = "demo-household"


def purchase(key="1", *, item="milk", stock=3, actual=1, planned=2):
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
    first = purchase(stock=1, actual=2)
    second = purchase("2", stock=1, actual=1)
    result = plan(first, second)
    assert len(result.shopping_updates) == len(result.transactions) == 2
    assert len(result.inventory_updates) == 1
    assert result.inventory_updates[0].record.quantity == 4
    assert result.inventory_updates[0].operation_ids == ("op-1", "op-2")
    assert [transaction.quantity_delta for transaction in result.transactions] == [2, 1]


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
        replace(second, inventory=second.inventory.model_copy(update={"quantity": 4}))
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


def test_fractional_member_aborts_entire_group_without_source_mutation():
    first, second = purchase(), purchase("2", actual=0.5)
    with pytest.raises(ShoppingPreviewError, match="integer count"):
        plan(first, second)
    assert first.inventory.quantity == 3
    assert first.shopping.status == "pending"


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


@pytest.mark.parametrize("planned,unit", [(None, None), (None, "瓶"), (2, None)])
def test_plan_and_removal_support_optional_intent_fields_without_fake_defaults(planned, unit):
    source = purchase()
    source = replace(
        source,
        shopping=source.shopping.model_copy(
            update={"quantity": planned, "unit": unit, "item_id": None}
        ),
        purchased_unit="瓶",
        resolved_inventory_item_id="milk",
    )
    result = plan(source)
    assert result.shopping_updates[0].record.quantity == planned
    assert result.shopping_updates[0].record.unit == unit
    assert result.shopping_updates[0].record.item_id is None
    assert result.shopping_updates[0].record.status == "completed"
    assert result.inventory_updates[0].record.quantity == 4
    assert type(result.inventory_updates[0].record.quantity) is int
    assert result.transactions[0].quantity_delta == 1
    assert removal(source).shopping_item_id == source.shopping.shopping_item_id
    assert source.shopping.quantity == planned


def test_name_only_purchase_without_an_actual_unit_is_deferred():
    source = purchase()
    source = replace(
        source,
        shopping=source.shopping.model_copy(
            update={"quantity": None, "unit": None},
        ),
    )
    with pytest.raises(ShoppingDecisionRequired, match="actual purchase unit"):
        plan(source)


def test_historical_purchase_uses_event_time_without_backdating_domain_updates():
    event_time = RECORDED - timedelta(days=1)
    source = replace(purchase(), purchased_at=event_time)
    result = plan(source)
    assert result.transactions[0].created_at == event_time
    assert result.shopping_updates[0].record.updated_at == RECORDED
    assert result.inventory_updates[0].record.updated_at == RECORDED
    assert result.shopping_updates[0].record.created_at == CREATED
    assert source.shopping.updated_at == source.inventory.updated_at == CREATED
    assert result.transactions[0].quantity_delta == 1
    assert result.inventory_updates[0].record.quantity == 4
    assert plan(source) == result


def test_distinct_purchase_times_survive_combined_inventory_update():
    first = replace(purchase(), purchased_at=RECORDED - timedelta(days=2))
    second = replace(purchase("2"), purchased_at=RECORDED - timedelta(days=1))
    result = plan(first, second)
    assert [entry.created_at for entry in result.transactions] == [
        first.purchased_at,
        second.purchased_at,
    ]
    assert len(result.inventory_updates) == 1
    assert result.inventory_updates[0].record.quantity == 5
    assert result.inventory_updates[0].record.updated_at == RECORDED
    assert all(row.record.updated_at == RECORDED for row in result.shopping_updates)


def test_purchase_with_no_event_time_uses_stored_confirmation_time():
    source = purchase()
    result = plan(source)
    assert source.purchased_at is None
    assert result.transactions[0].created_at == RECORDED
    # Same stored confirmation/selection on a retry, never datetime.now().
    assert plan(source, recorded_at=RECORDED) == result


@pytest.mark.parametrize("value", [datetime(2026, 11, 2), "2026-11-02", 0])
def test_purchase_event_time_requires_a_timezone_aware_datetime(value):
    source = replace(purchase(), purchased_at=value)
    with pytest.raises(ShoppingPreviewError, match="timezone-aware"):
        plan(source)


def test_purchase_time_after_confirmation_rejects_the_entire_group():
    first = purchase()
    future = replace(purchase("2"), purchased_at=RECORDED + timedelta(seconds=1))
    with pytest.raises(ShoppingPreviewError, match="after confirmation"):
        plan(first, future)
    assert first.inventory.quantity == 3
    assert first.shopping.status == "pending"


def test_purchase_time_comparison_handles_timezone_offsets_at_the_same_instant():
    from datetime import timezone

    source = replace(purchase(), purchased_at=RECORDED.astimezone(timezone(timedelta(hours=8))))
    result = plan(source)
    assert result.transactions[0].created_at == RECORDED
    assert result.shopping_updates[0].record.updated_at == RECORDED


def new_purchase(key="new", **changes):
    from app.services.shopping.planning import NewInventoryTarget

    source = purchase(key)
    return replace(
        source,
        **{
            "shopping": source.shopping.model_copy(update={"item_id": None}),
            "inventory": None,
            "inventory_etag": None,
            "new_inventory": NewInventoryTarget(
                item_id=f"new-{key}", name="洗碗精", unit="瓶", location="廚房水槽下"
            ),
            **changes,
        },
    )


def test_new_target_plans_create_actual_stock_without_placeholder_or_fake_etag():
    source = new_purchase(purchased_at=RECORDED - timedelta(days=1))
    result = plan(purchase(), source)
    assert len(result.inventory_creates) == len(result.inventory_updates) == 1
    created = result.inventory_creates[0]
    assert created.record.item_id == source.new_inventory.item_id
    assert created.operation_id == source.operation_id
    assert created.record.name == "洗碗精"
    assert created.record.unit == "瓶"
    assert created.record.location == "廚房水槽下"
    assert created.record.quantity == 1
    assert created.record.created_at == created.record.updated_at == RECORDED
    assert result.transactions[1].item_id == created.record.item_id
    assert result.transactions[1].created_at == source.purchased_at
    assert result.shopping_updates[1].record.status == "completed"
    assert result.shopping_updates[1].record.quantity == 2
    assert source.shopping.status == "pending"
    assert source.inventory is None
    assert plan(purchase(), source) == result


@pytest.mark.parametrize(
    "changes",
    [
        {"inventory": purchase().inventory},
        {"inventory_etag": "fake"},
        {"shopping": purchase().shopping},
        {"resolved_inventory_item_id": "other"},
        {"purchased_unit": "箱"},
        {"purchased_quantity": 1.5},
        {"purchased_quantity": 0},
        {"purchased_at": RECORDED + timedelta(seconds=1)},
    ],
)
def test_invalid_new_target_aborts_the_group_without_mutation(changes):
    first = purchase()
    with pytest.raises(ShoppingPreviewError):
        plan(first, new_purchase(**changes))
    assert first.inventory.quantity == 3
    assert first.shopping.status == "pending"


@pytest.mark.parametrize("field", ["item_id", "name", "unit", "location"])
def test_new_target_requires_resolved_nonempty_values(field):
    source = new_purchase()
    with pytest.raises(ShoppingPreviewError):
        plan(replace(source, new_inventory=replace(source.new_inventory, **{field: " "})))


def test_new_target_supports_absent_planned_values_without_defaulting():
    source = new_purchase()
    source = replace(
        source, shopping=source.shopping.model_copy(update={"quantity": None, "unit": None})
    )
    result = plan(source)
    assert result.inventory_creates[0].record.quantity == 1
    assert result.shopping_updates[0].record.quantity is None
    assert result.shopping_updates[0].record.unit is None


def test_new_target_cannot_share_an_id_with_an_existing_or_another_new_target():
    first = new_purchase()
    second = new_purchase("second", new_inventory=first.new_inventory)
    with pytest.raises(ShoppingPreviewError):
        plan(first, second)
    existing = purchase(item=first.new_inventory.item_id)
    for members in [(existing, first), (first, existing)]:
        with pytest.raises(ShoppingPreviewError):
            plan(*members)


@pytest.mark.skipif(
    ShoppingItem.model_fields["quantity"].is_required(),
    reason="B1's optional Shopping Intent schema is in pending PR #64; run joint validation",
)
def test_real_name_only_pydantic_model_supports_existing_and_new_inventory_targets():
    # This runs when #64's actual schema is present, rather than copying an invalid model.
    shopping = ShoppingItem(
        shopping_item_id="name-only",
        household_id=HOUSEHOLD,
        name="牛奶",
        status="pending",
        created_at=CREATED,
        updated_at=CREATED,
    )
    assert shopping.quantity is None and shopping.unit is None and shopping.item_id is None
    for source in (
        replace(
            purchase(), shopping=shopping, purchased_unit="瓶", resolved_inventory_item_id="milk"
        ),
        replace(new_purchase(), shopping=shopping),
    ):
        result = plan(source)
        completed = result.shopping_updates[0].record
        assert completed.quantity is None and completed.unit is None and completed.item_id is None
        assert completed.status == "completed"
        assert result.transactions[0].quantity_delta == 1
        assert type(result.transactions[0].quantity_delta) is int
        assert removal(source).shopping_item_id == shopping.shopping_item_id
        assert plan(source) == result
