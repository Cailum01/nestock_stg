"""A1's purchase-card resolver, alone and with C1's builder, A1's service and B1's batch.

Only the Cosmos container is faked (the same fake as `test_approvals_cosmos_write`).
"""

import asyncio
from datetime import timedelta

import pytest
from test_approvals_cosmos_write import T0, Clock, FakeCosmosContainer

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import InventoryItem, ShoppingItem
from app.services.approvals import (
    ApprovalService,
    ApprovalStatus,
    NewOperation,
    ResumeOutcome,
    TargetChanged,
    TargetVersion,
)
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_write import CosmosWriteRepository
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.purchase_inputs import (
    COMPLETE_PURCHASE,
    CREATE_ITEM,
    CardInputResolver,
    UnsupportedAction,
)
from app.services.shopping.planning import NewInventoryTarget
from app.services.shopping.workflow import ShoppingWriteBuilder

HOME = "home"


def run(coro):
    return asyncio.run(coro)


class Home:
    """One household in the fake container: 衛生紙 1 包 at home, entries on the list."""

    def __init__(self, entries=1):
        self.container = FakeCosmosContainer()
        self.reader = CosmosReadRepository(self.container)
        self.resolver = CardInputResolver(self.reader)
        self.clock = Clock()
        self.service = ApprovalService(
            CosmosWriteRepository(self.container, query_client=self.container),
            ShoppingWriteBuilder(self.container, self.resolver),
            clock=self.clock,
        )
        self.inventory = InventoryItem(
            item_id="tissue",
            household_id=HOME,
            name="衛生紙",
            quantity=1,
            unit="包",
            location="浴室櫃子",
            created_at=T0,
            updated_at=T0,
        )
        self.inventory_etag = self._put(self.inventory)
        self.shopping = []
        for index in range(entries):
            entry = ShoppingItem(
                shopping_item_id=f"tissue-{index}",
                household_id=HOME,
                name="衛生紙",
                quantity=2,
                unit="包",
                status="pending",
                item_id="tissue",
                created_at=T0,
                updated_at=T0,
            )
            self.shopping.append((entry, self._put(entry)))

    def _put(self, record):
        document = CosmosDocumentMapper.to_document(record)
        document["deleted_at"] = None
        return self.container.put(document)

    def line(self, index=0, **changes):
        """A card line in the format of A1's `purchase_operation` (`app/agent/cards.py`)."""
        entry, etag = self.shopping[index]
        values = {
            "name": "衛生紙",
            "purchased_quantity": 2,
            "purchased_unit": "包",
            "shopping_item_id": entry.shopping_item_id,
            "item_id": "tissue",
            "transaction_id": f"tx-{index}",
        }
        values.update(changes.pop("values", {}))
        return NewOperation(
            action=changes.pop("action", COMPLETE_PURCHASE),
            description="衛生紙 +2 包（從待購清單完成購買）",
            values={key: value for key, value in values.items() if value is not None},
            targets=changes.pop(
                "targets",
                [
                    TargetVersion(record_id=f"shopping_item:{entry.shopping_item_id}", etag=etag),
                    TargetVersion(record_id="inventory_item:tissue", etag=self.inventory_etag),
                ],
            ),
        )

    def card(self, *lines):
        return self.service.prepare(
            household_id=HOME,
            presentation=ApprovalPresentation.SINGLE_ACTION
            if len(lines) == 1
            else ApprovalPresentation.BATCH_ACTION,
            operations=lines,
        )

    def confirmed(self, card, approved=None):
        """The card as the service hands it to the builder at confirmation."""
        return card.model_copy(
            update={
                "status": ApprovalStatus.CONFIRMED,
                "approved_operation_ids": approved or card.operation_ids,
                "decided_at": T0 + timedelta(minutes=1),
            }
        )

    def change(self, document_id, **fields):
        document = dict(self.container.get(document_id))
        document.pop("_etag")
        document.update(fields)
        self.container.put(document)


# ----- the resolver on its own --------------------------------------------------------


def test_a_stored_purchase_line_becomes_c1_input_with_the_card_values():
    home = Home()
    card = home.card(home.line())

    changes = run(home.resolver.resolve(home.confirmed(card)))

    assert changes.removals == ()
    (purchase,) = changes.purchases
    entry, entry_etag = home.shopping[0]
    assert purchase.operation_id == card.operations[0].operation_id
    assert purchase.transaction_id == "tx-0"
    assert purchase.shopping == entry and purchase.shopping_etag == entry_etag
    assert purchase.inventory == home.inventory
    assert purchase.inventory_etag == home.inventory_etag
    assert purchase.purchased_quantity == 2 and isinstance(purchase.purchased_quantity, int)
    assert purchase.purchased_unit == "包"
    assert purchase.resolved_inventory_item_id == "tissue"
    assert purchase.purchased_at is None
    assert purchase.new_inventory is None


def test_only_approved_lines_are_resolved_in_card_order():
    home = Home(entries=3)
    card = home.card(home.line(0), home.line(1), home.line(2))
    first, _, third = card.operation_ids

    changes = run(home.resolver.resolve(home.confirmed(card, approved=[third, first])))

    assert [row.operation_id for row in changes.purchases] == [first, third]
    assert [row.shopping.shopping_item_id for row in changes.purchases] == ["tissue-0", "tissue-2"]


@pytest.mark.parametrize("document_id", ["shopping_item:tissue-0", "inventory_item:tissue"])
def test_a_record_changed_after_the_card_raises_target_changed(document_id):
    home = Home()
    card = home.card(home.line())
    home.change(document_id, updated_at="2026-11-09T06:30:00.000000Z")

    with pytest.raises(TargetChanged):
        run(home.resolver.resolve(home.confirmed(card)))


@pytest.mark.parametrize("document_id", ["shopping_item:tissue-0", "inventory_item:tissue"])
def test_a_deleted_record_raises_target_changed(document_id):
    home = Home()
    card = home.card(home.line())
    home.change(document_id, deleted_at="2026-11-09T06:30:00.000000Z")

    with pytest.raises(TargetChanged):
        run(home.resolver.resolve(home.confirmed(card)))


@pytest.mark.parametrize("action", ["confirm_item_match", "remove_shopping"])
def test_lines_other_than_complete_purchase_are_refused(action):
    home = Home()
    card = home.card(home.line(action=action))

    with pytest.raises(UnsupportedAction):
        run(home.resolver.resolve(home.confirmed(card)))


@pytest.mark.parametrize(
    "values",
    [
        {"purchased_quantity": 2.5},
        {"purchased_quantity": "2"},
        {"purchased_quantity": True},
        {"purchased_quantity": 0},
        {"purchased_unit": None},
        {"purchased_unit": " "},
        {"transaction_id": None},
        {"purchased_at": "2026-11-09T05:00:00"},
        {"purchased_at": "昨天"},
    ],
)
def test_bad_card_values_are_refused(values):
    home = Home()
    card = home.card(home.line(values=values))

    with pytest.raises(ValueError):
        run(home.resolver.resolve(home.confirmed(card)))


def test_a_line_must_carry_exactly_its_two_record_versions():
    home = Home()
    entry, etag = home.shopping[0]
    only_shopping = [TargetVersion(record_id=f"shopping_item:{entry.shopping_item_id}", etag=etag)]
    card = home.card(home.line(targets=only_shopping))

    with pytest.raises(ValueError):
        run(home.resolver.resolve(home.confirmed(card)))


def test_a_purchase_time_on_the_card_is_passed_through():
    home = Home()
    card = home.card(home.line(values={"purchased_at": "2026-11-08T04:00:00+00:00"}))

    (purchase,) = run(home.resolver.resolve(home.confirmed(card))).purchases

    assert purchase.purchased_at == T0 - timedelta(days=1, hours=2)


# ----- with C1's builder, A1's approval service and B1's batch -------------------------


def resume(home, card, approved=None):
    return run(
        home.service.resume(
            ResumeRequest(
                household_id=HOME,
                approval_id=card.approval_id,
                approved=True,
                approved_operation_ids=approved,
            )
        )
    )


def test_a_confirmed_card_is_written_through_c1_builder_in_one_b1_batch():
    home = Home()
    card = run(home.service.save(home.card(home.line())))
    assert home.container.get("inventory_item:tissue")["quantity"] == 1

    home.clock.advance(1)
    result = resume(home, card)

    assert result.outcome == ResumeOutcome.COMPLETED
    assert home.container.get("inventory_item:tissue")["quantity"] == 3
    assert home.container.get("shopping_item:tissue-0")["status"] == "completed"
    (transaction,) = home.container.of_type("transaction")
    assert transaction["transaction_id"] == "tx-0"
    assert transaction["quantity_delta"] == 2
    assert transaction["created_at"] == "2026-11-09T06:01:00.000000Z"
    assert len(home.container.of_type("change_event")) == 1
    assert len(home.container.of_type("operation_receipt")) == 1

    batches = len(home.container.batches)
    again = resume(home, card)
    assert again.outcome == ResumeOutcome.ALREADY_COMPLETED
    assert len(home.container.batches) == batches
    assert home.container.get("inventory_item:tissue")["quantity"] == 3


def test_a_partly_approved_batch_card_writes_only_the_approved_line():
    home = Home(entries=2)
    card = run(home.service.save(home.card(home.line(0), home.line(1))))
    _, second = card.operation_ids

    result = resume(home, card, approved=[second])

    assert result.outcome == ResumeOutcome.COMPLETED
    assert home.container.get("shopping_item:tissue-0")["status"] == "pending"
    assert home.container.get("shopping_item:tissue-1")["status"] == "completed"
    assert home.container.get("inventory_item:tissue")["quantity"] == 3
    assert [tx["transaction_id"] for tx in home.container.of_type("transaction")] == ["tx-1"]


def test_a_record_changed_after_saving_needs_a_new_card_and_writes_nothing():
    home = Home()
    card = run(home.service.save(home.card(home.line())))
    home.change("inventory_item:tissue", quantity=5)

    result = resume(home, card)

    assert result.outcome == ResumeOutcome.NEEDS_NEW_CARD
    assert home.container.get("inventory_item:tissue")["quantity"] == 5
    assert home.container.get("shopping_item:tissue-0")["status"] == "pending"
    assert home.container.of_type("transaction") == []
    assert home.container.of_type("operation_receipt") == []


# ----- a list entry the household does not have yet (Issue #90) -----------------------


def new_entry(home, unit="條"):
    """牙膏 on the list, not linked to an inventory item: the household has none."""
    entry = ShoppingItem(
        shopping_item_id="paste-1",
        household_id=HOME,
        name="牙膏",
        quantity=1,
        unit=unit,
        status="pending",
        created_at=T0,
        updated_at=T0,
    )
    return entry, home._put(entry)


def new_item_line(entry, etag, targets=None, **values):
    """A `create_item` line in the format of A1's `new_item_operation`."""
    line = {
        "name": "牙膏",
        "quantity": 1,
        "unit": "條",
        "location": "浴室櫃子",
        "shopping_item_id": entry.shopping_item_id,
        "transaction_id": "tx-paste",
    }
    line.update(values)
    return NewOperation(
        action=CREATE_ITEM,
        description="會新增一個庫存品項：牙膏 1 條，放在浴室櫃子（從待購清單完成購買）",
        values={key: value for key, value in line.items() if value is not None},
        targets=targets
        or [TargetVersion(record_id=f"shopping_item:{entry.shopping_item_id}", etag=etag)],
    )


def test_a_new_item_line_becomes_c1_new_inventory_target_with_b1_stable_id():
    home = Home()
    entry, etag = new_entry(home)
    card = home.card(new_item_line(entry, etag))
    (operation_id,) = card.operation_ids

    (purchase,) = run(home.resolver.resolve(home.confirmed(card))).purchases

    item_id = resolve_inventory_item_id(operation_id)
    assert purchase.new_inventory == NewInventoryTarget(item_id, "牙膏", "條", "浴室櫃子")
    assert purchase.inventory is None and purchase.inventory_etag is None
    assert purchase.resolved_inventory_item_id == item_id
    assert (purchase.shopping, purchase.shopping_etag) == (entry, etag)
    assert (purchase.purchased_quantity, purchase.purchased_unit) == (1, "條")
    assert purchase.transaction_id == "tx-paste"


@pytest.mark.parametrize(
    "values",
    [
        {"quantity": 0},
        {"quantity": 1.0},
        {"unit": None},
        {"location": None},
        {"location": " "},
        {"name": None},
        {"transaction_id": None},
    ],
)
def test_bad_new_item_values_are_refused(values):
    home = Home()
    entry, etag = new_entry(home)
    card = home.card(new_item_line(entry, etag, **values))

    with pytest.raises(ValueError):
        run(home.resolver.resolve(home.confirmed(card)))


def test_a_new_item_line_carries_only_its_shopping_version():
    home = Home()
    entry, etag = new_entry(home)
    both = [
        TargetVersion(record_id="shopping_item:paste-1", etag=etag),
        TargetVersion(record_id="inventory_item:tissue", etag=home.inventory_etag),
    ]
    card = home.card(new_item_line(entry, etag, targets=both))

    with pytest.raises(ValueError):
        run(home.resolver.resolve(home.confirmed(card)))


def test_an_entry_changed_after_a_new_item_card_raises_target_changed():
    home = Home()
    entry, etag = new_entry(home)
    card = home.card(new_item_line(entry, etag))
    home.change("shopping_item:paste-1", quantity=3)

    with pytest.raises(TargetChanged):
        run(home.resolver.resolve(home.confirmed(card)))


def test_a_confirmed_new_item_card_creates_the_item_once_in_one_batch():
    home = Home()
    entry, etag = new_entry(home)
    card = run(home.service.save(home.card(new_item_line(entry, etag))))
    item_id = resolve_inventory_item_id(card.operation_ids[0])
    assert home.container.get(f"inventory_item:{item_id}") is None

    home.clock.advance(1)
    result = resume(home, card)

    assert result.outcome == ResumeOutcome.COMPLETED
    created = home.container.get(f"inventory_item:{item_id}")
    assert (created["name"], created["quantity"], created["unit"], created["location"]) == (
        "牙膏",
        1,
        "條",
        "浴室櫃子",
    )
    assert home.container.get("shopping_item:paste-1")["status"] == "completed"
    (transaction,) = home.container.of_type("transaction")
    assert (transaction["item_id"], transaction["quantity_delta"]) == (item_id, 1)
    assert len(home.container.of_type("change_event")) == 1
    assert home.container.get("inventory_item:tissue")["quantity"] == 1

    batches = len(home.container.batches)
    again = resume(home, card)
    assert again.outcome == ResumeOutcome.ALREADY_COMPLETED
    assert len(home.container.batches) == batches
    assert len(home.container.of_type("inventory_item")) == 2
