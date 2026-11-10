"""Real C1 builder + A1 ApprovalService + B1 repository, only container/A1 resolver faked."""

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_approvals_cosmos_write import T0, Clock, FakeCosmosContainer

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import InventoryItem, ShoppingItem
from app.services.approvals import (
    ApprovalNotFound,
    ApprovalService,
    BatchTooLarge,
    NewOperation,
    ResumeOutcome,
    TargetVersion,
)
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_write import CosmosWriteRepository, ReplaceDocument
from app.services.shopping.planning import NewInventoryTarget, PurchaseInput
from app.services.shopping.preview import ShoppingPreviewError
from app.services.shopping.workflow import (
    RemovalInput,
    ResolvedShoppingChanges,
    ShoppingWriteBuilder,
)

HOME = "home"


def run(coro):
    return asyncio.run(coro)


class StoredInputResolver:
    """Test stand-in for A1: binds each card ID to original typed inputs, never client values."""

    def __init__(self):
        self.inputs = {}

    async def resolve(self, record):
        selected = [self.inputs[key] for key in record.approved_operation_ids]
        # The production A1 adapter must also bind all displayed fields/action types.
        for selected_id, entry in zip(record.approved_operation_ids, selected):
            operation = next(op for op in record.operations if op.operation_id == selected_id)
            if isinstance(entry, PurchaseInput):
                assert operation.values["quantity"] == entry.purchased_quantity
                assert operation.values["transaction_id"] == entry.transaction_id
        return ResolvedShoppingChanges(
            purchases=tuple(row for row in selected if isinstance(row, PurchaseInput)),
            removals=tuple(row for row in selected if isinstance(row, RemovalInput)),
        )


def setup(kinds=("existing",), *, save=True):
    container = FakeCosmosContainer()
    repo = CosmosWriteRepository(container, query_client=container)
    resolver, clock = StoredInputResolver(), Clock()
    builder = ShoppingWriteBuilder(container, resolver)
    service = ApprovalService(repo, builder, clock=clock)
    inventory = InventoryItem(
        item_id="milk",
        household_id=HOME,
        name="牛奶",
        quantity=3,
        unit="瓶",
        created_at=T0,
        updated_at=T0,
        location="冰箱",
        aliases=["鮮奶"],
    )
    inv_doc = CosmosDocumentMapper.to_document(inventory)
    inv_doc["deleted_at"] = None
    inv_etag = container.put(inv_doc)
    sources, operations = [], []
    for index, kind in enumerate(kinds):
        shopping = ShoppingItem(
            shopping_item_id=f"shopping-{index}",
            household_id=HOME,
            name="牛奶",
            quantity=2,
            unit="瓶",
            status="pending",
            item_id="milk" if kind == "existing" else None,
            created_at=T0,
            updated_at=T0,
        )
        document = CosmosDocumentMapper.to_document(shopping)
        document["deleted_at"] = None
        etag = container.put(document)
        targets = [TargetVersion(record_id=document["id"], etag=etag)]
        if kind == "existing":
            targets.append(TargetVersion(record_id=inv_doc["id"], etag=inv_etag))
        tx_id = f"transaction-{index}"
        operations.append(
            NewOperation(
                action="remove_shopping" if kind == "remove" else "complete_purchase",
                description="已解析的購買或移除",
                values={"quantity": 1, "transaction_id": tx_id},
                targets=targets,
            )
        )
        sources.append((shopping, etag, tx_id, kind))
    card = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION
        if len(kinds) == 1
        else ApprovalPresentation.BATCH_ACTION,
        operations=operations,
    )
    for op, (shopping, etag, tx_id, kind) in zip(card.operations, sources):
        if kind == "remove":
            entry = RemovalInput(op.operation_id, shopping, etag)
        else:
            entry = PurchaseInput(
                operation_id=op.operation_id,
                transaction_id=tx_id,
                shopping=shopping,
                shopping_etag=etag,
                inventory=inventory if kind == "existing" else None,
                inventory_etag=inv_etag if kind == "existing" else None,
                purchased_quantity=1,
                purchased_at=T0 - timedelta(days=1),
                new_inventory=NewInventoryTarget(f"new-{op.operation_id}", "洗碗精", "瓶", "水槽下")
                if kind == "new"
                else None,
            )
        resolver.inputs[op.operation_id] = entry
    if save:
        run(service.save(card))
    return SimpleNamespace(
        service=service,
        container=container,
        repo=repo,
        resolver=resolver,
        clock=clock,
        builder=builder,
        card=card,
    )


def request(h, *, approve=True, selected=None, household=HOME):
    return ResumeRequest(
        household_id=household,
        approval_id=h.card.approval_id,
        approved=approve,
        approved_operation_ids=(selected if selected is not None else h.card.operation_ids)
        if h.card.presentation == ApprovalPresentation.BATCH_ACTION
        else None,
    )


def receipts(h):
    return h.container.of_type("operation_receipt")


def test_complete_purchase_merges_stock_and_commits_events_receipts_approval_atomically():
    h = setup(("existing", "existing"))
    result = run(h.service.resume(request(h)))
    assert result.outcome == ResumeOutcome.COMPLETED
    assert h.container.get("inventory_item:milk")["quantity"] == 5
    assert h.container.get("inventory_item:milk")["aliases"] == ["鮮奶"]
    assert h.container.get("inventory_item:milk")["deleted_at"] is None
    assert len(receipts(h)) == 2
    assert len(h.container.of_type("transaction")) == len(h.container.of_type("change_event")) == 2
    for doc in h.container.of_type("shopping_item"):
        assert doc["quantity"] == 2 and doc["status"] == "completed"
        assert doc["deleted_at"] is None
    batch = h.container.batches[-1]
    assert len(batch) == 10  # receipts + 2*(Shopping, Transaction, event) + Inventory + approval
    assert batch[-1].document["status"] == "completed"
    assert sum(op.document["record_type"] == "inventory_item" for op in batch) == 1
    assert all(op.if_match_etag for op in batch if isinstance(op, ReplaceDocument))
    assert all("_etag" not in op.document for op in batch)
    before = len(h.container.batches)
    retried = run(h.service.resume(request(h)))
    assert retried.outcome == ResumeOutcome.ALREADY_COMPLETED
    assert retried.record.outcome == result.record.outcome
    assert len(h.container.batches) == before


def test_new_inventory_and_existing_purchase_commit_together_with_stable_time_and_ids():
    h = setup(("existing", "new"))
    new = h.resolver.inputs[h.card.operation_ids[1]]
    assert h.container.get(f"inventory_item:{new.new_inventory.item_id}") is None
    h.clock.advance(1)
    h.container.lose_next_domain_batch_response = True
    result = run(h.service.resume(request(h)))
    assert result.written
    created = h.container.get(f"inventory_item:{new.new_inventory.item_id}")
    assert created["quantity"] == 1 and created["name"] == "洗碗精"
    assert created["location"] == "水槽下"
    assert created["created_at"] == created["updated_at"]
    event = h.container.get(f"change_event:{new.operation_id}")
    assert event["item_id"] == created["item_id"]
    assert event["occurred_at"] != event["recorded_at"]
    assert event["transaction_id"] == new.transaction_id
    assert h.container.get("inventory_item:milk")["quantity"] == 4
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.ALREADY_COMPLETED
    assert len(h.container.of_type("inventory_item")) == 2
    assert len(receipts(h)) == 2


@pytest.mark.parametrize("kind", ["existing", "new", "remove"])
def test_cancel_does_not_write_domain_data(kind):
    h = setup((kind,))
    before = [dict(doc) for doc in h.container.of_type("shopping_item")]
    assert run(h.service.resume(request(h, approve=False))).outcome == ResumeOutcome.CANCELLED
    assert h.container.of_type("shopping_item") == before
    assert len(h.container.of_type("inventory_item")) == 1
    assert not receipts(h) and not h.container.of_type("transaction")


def test_partial_selection_does_not_create_or_complete_unselected_target():
    h = setup(("existing", "new"))
    assert run(h.service.resume(request(h, selected=[h.card.operation_ids[0]]))).written
    assert h.container.get("shopping_item:shopping-1")["status"] == "pending"
    assert len(h.container.of_type("inventory_item")) == 1
    assert len(receipts(h)) == 1


def test_removal_only_tombstones_pending_item_without_inventory_or_transaction_changes():
    h = setup(("remove",))
    inventory = dict(h.container.get("inventory_item:milk"))
    assert run(h.service.resume(request(h))).written
    document = h.container.get("shopping_item:shopping-0")
    assert document["status"] == "pending" and document["quantity"] == 2
    assert document["deleted_at"] is not None
    assert h.container.get("inventory_item:milk") == inventory
    assert not h.container.of_type("transaction") and not h.container.of_type("change_event")
    assert len(receipts(h)) == 1


@pytest.mark.parametrize("kind", ["existing", "new", "remove"])
def test_stale_shopping_etag_rejects_whole_card(kind):
    h = setup((kind,))
    document = dict(h.container.get("shopping_item:shopping-0"))
    document["name"] = "changed"
    h.container.put(document)
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.NEEDS_NEW_CARD
    assert not receipts(h) and not h.container.of_type("transaction")
    assert h.container.get("inventory_item:milk")["quantity"] == 3


def test_conflict_during_batch_rolls_back_receipts_shopping_events_and_new_inventory():
    h = setup(("existing", "new"))

    def race():
        h.container.put(dict(h.container.get("inventory_item:milk")))

    h.container.before_next_domain_batch = race
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.NEEDS_NEW_CARD
    assert not receipts(h) and not h.container.of_type("transaction")
    assert not h.container.of_type("change_event")
    assert len(h.container.of_type("inventory_item")) == 1
    assert all(doc["status"] == "pending" for doc in h.container.of_type("shopping_item"))


def test_new_item_id_collision_at_commit_is_atomic_and_never_overwrites():
    h = setup(("new",))
    entry = next(iter(h.resolver.inputs.values()))
    item_id = entry.new_inventory.item_id

    def race():
        h.container.put(
            {
                "id": f"inventory_item:{item_id}",
                "household_id": HOME,
                "record_type": "inventory_item",
                "item_id": item_id,
                "quantity": 99,
            }
        )

    h.container.before_next_domain_batch = race
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.NEEDS_NEW_CARD
    assert h.container.get(f"inventory_item:{item_id}")["quantity"] == 99
    assert not receipts(h) and not h.container.of_type("transaction")
    assert h.container.get("shopping_item:shopping-0")["status"] == "pending"


def test_unknown_household_cannot_resume_or_write():
    h = setup()
    with pytest.raises(ApprovalNotFound):
        run(h.service.resume(request(h, household="other")))
    assert not receipts(h)


def test_partial_receipts_on_confirmed_card_stop_without_replaying():
    h = setup(("existing", "new"))
    # Simulate corruption, not a legal partial commit: manually confirm and insert a receipt.
    stored = h.container.get(f"pending_approval:{h.card.approval_id}")
    confirmed = dict(
        stored,
        status="confirmed",
        decided_at=T0.isoformat(),
        approved_operation_ids=h.card.operation_ids,
    )
    h.container.put(confirmed)
    op_id = h.card.operation_ids[0]
    h.container.put(
        {
            "id": "operation:" + hashlib.sha256(op_id.encode()).hexdigest(),
            "record_type": "operation_receipt",
            "household_id": HOME,
            "operation_id": op_id,
            "request_fingerprint": "a" * 64,
            "batch_fingerprint": "b" * 64,
        }
    )
    count = len(h.container.batches)
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.INCONSISTENT
    assert len(h.container.batches) == count
    assert not h.container.of_type("transaction")


def test_save_rejects_oversized_card_before_any_approval_or_domain_write():
    h = setup(("new",) * 20, save=False)  # 20*(receipt + 4 writes) + approval = 101
    with pytest.raises(BatchTooLarge):
        run(h.service.save(h.card))
    assert not h.container.of_type("pending_approval")
    assert not receipts(h) and not h.container.of_type("transaction")
    assert len(h.container.of_type("inventory_item")) == 1


def test_exact_hundred_action_batch_is_accepted_including_receipts_and_approval():
    h = setup(("existing",) * 22 + ("new",) * 2)
    assert run(h.service.resume(request(h))).written
    assert len(h.container.batches[-1]) == 100


@pytest.mark.parametrize("mismatch", ["selection", "etag", "snapshot", "scope"])
def test_resolver_cannot_substitute_selection_target_versions_or_snapshots(mismatch):
    h = setup()
    key = h.card.operation_ids[0]
    entry = h.resolver.inputs[key]
    if mismatch == "selection":
        entry = replace(entry, operation_id="other")
    elif mismatch == "etag":
        entry = replace(entry, inventory_etag="other")
    else:
        field, value = ("quantity", 99) if mismatch == "snapshot" else ("household_id", "other")
        entry = replace(entry, inventory=entry.inventory.model_copy(update={field: value}))
    h.resolver.inputs[key] = entry
    with pytest.raises((ShoppingPreviewError, ValueError)):
        run(h.service.resume(request(h)))
    assert not receipts(h)


def test_uncommitted_timeout_retries_with_original_ids_times_and_fixed_selection():
    h = setup(("existing", "new"))
    execute = h.container.execute_batch
    failed = False

    async def fail_once(*, partition_key, operations):
        nonlocal failed
        if not failed and any(
            op.document.get("record_type") == "operation_receipt" for op in operations
        ):
            failed = True
            raise TimeoutError("Nothing committed; response unavailable")
        return await execute(partition_key=partition_key, operations=operations)

    h.container.execute_batch = fail_once
    first = run(h.service.resume(request(h)))
    assert first.outcome == ResumeOutcome.NOT_WRITTEN
    assert not receipts(h) and not h.container.of_type("transaction")
    stored = h.container.get(f"pending_approval:{h.card.approval_id}")
    assert stored["status"] == "confirmed"
    decision_time = stored["decided_at"]
    h.clock.advance(45)  # Confirmed stock writes may retry beyond the initial card expiry.
    result = run(h.service.resume(request(h)))
    assert result.written and result.record.decided_at == datetime.fromisoformat(decision_time)
    assert len(receipts(h)) == 2
    for event in h.container.of_type("change_event"):
        assert datetime.fromisoformat(event["recorded_at"]) == datetime.fromisoformat(decision_time)
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.ALREADY_COMPLETED


def test_removal_lost_response_does_not_repeat_tombstone_or_create_transactions():
    h = setup(("remove",))
    h.container.lose_next_domain_batch_response = True
    assert run(h.service.resume(request(h))).written
    saved = dict(h.container.get("shopping_item:shopping-0"))
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.ALREADY_COMPLETED
    assert h.container.get("shopping_item:shopping-0") == saved
    assert len(receipts(h)) == 1 and not h.container.of_type("transaction")


def test_stored_card_targets_cannot_be_dropped_to_bypass_version_binding():
    h = setup(save=False)
    op = h.card.operations[0].model_copy(update={"targets": []})
    card = h.card.model_copy(update={"operations": [op]})
    with pytest.raises(ShoppingPreviewError, match="target ETags"):
        run(h.service.save(card))
    assert not h.container.of_type("pending_approval") and not receipts(h)


def test_existing_new_item_collision_is_rejected_before_storing_card():
    h = setup(("new",), save=False)
    entry = next(iter(h.resolver.inputs.values()))
    h.container.put({"id": f"inventory_item:{entry.new_inventory.item_id}", "household_id": HOME})
    from app.services.approvals import TargetChanged

    with pytest.raises(TargetChanged):
        run(h.service.save(h.card))
    assert not h.container.of_type("pending_approval") and not receipts(h)


def test_resolver_order_does_not_change_the_canonical_shared_write_plan():
    h = setup(("existing", "existing", "new"), save=False)
    trial = h.card.model_copy(
        update={"decided_at": T0, "approved_operation_ids": h.card.operation_ids}
    )
    normal = run(h.builder.build(trial))
    original = h.resolver.resolve

    async def reversed_resolver(record):
        result = await original(record)
        return replace(result, purchases=tuple(reversed(result.purchases)))

    h.resolver.resolve = reversed_resolver
    assert run(h.builder.build(trial)) == normal
