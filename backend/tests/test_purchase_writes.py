"""A1 purchase adapter (v8 flow C, Issue #60) on B1's real read and write repositories.

Only the Cosmos container underneath is faked; every write gives the document a new
`_etag`, the way Cosmos does, so a record changed by someone else is detected.
The card is made with `purchase_operation`, saved and confirmed through the approval
service, and written by `PurchaseWriteBuilder` through C1's purchase plan.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus
from app.services.approvals import ApprovalService, NewOperation, ResumeOutcome
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    ReplaceDocument,
)
from app.services.purchase_writes import (
    COMPLETE_PURCHASE,
    PurchaseWriteBuilder,
    UnsupportedAction,
    purchase_operation,
)
from app.services.shopping.preview import ShoppingDecisionRequired

HOME = "home"
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
CREATED = datetime(2026, 11, 1, tzinfo=timezone.utc)


class FakeCosmosContainer:
    """One container: batch, point read and query; each write gets a new ETag."""

    def __init__(self):
        self.documents = {}
        self.domain_batches = []
        self._version = 0
        self.lose_next_domain_batch_response = False

    def put(self, record):
        document = CosmosDocumentMapper.to_document(record)
        self._version += 1
        self.documents[(record.household_id, document["id"])] = {
            **document,
            "_etag": f'"etag-{self._version}"',
        }

    def get(self, document_id):
        return self.documents.get((HOME, document_id))

    def of_type(self, record_type):
        return [doc for doc in self.documents.values() if doc.get("record_type") == record_type]

    async def execute_batch(self, *, partition_key, operations):
        await asyncio.sleep(0)
        operations = tuple(operations)
        staged = {}
        for index, operation in enumerate(operations):
            document = dict(operation.document)
            key = (partition_key, document["id"])
            current = staged.get(key) or self.documents.get(key)
            if isinstance(operation, CreateDocument) and current is not None:
                raise BatchOperationConflict(index)
            if isinstance(operation, ReplaceDocument) and (
                current is None or current["_etag"] != operation.if_match_etag
            ):
                raise BatchOperationConflict(index)
            self._version += 1
            staged[key] = {**document, "_etag": f'"etag-{self._version}"'}
        self.documents.update(staged)
        domain_batch = any(
            op.document.get("record_type") == "operation_receipt" for op in operations
        )
        if domain_batch:
            self.domain_batches.append(operations)
            if self.lose_next_domain_batch_response:
                self.lose_next_domain_batch_response = False
                raise TimeoutError("Lost response after the batch committed.")

    async def read_item(self, *, item_id, partition_key):
        await asyncio.sleep(0)
        document = self.documents.get((partition_key, item_id))
        return dict(document) if document is not None else None

    async def query_items(self, *, query, parameters, partition_key):
        await asyncio.sleep(0)
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        statuses = {value for name, value in values.items() if name.endswith("_status")}
        return [
            dict(document)
            for (household, _), document in self.documents.items()
            if household == partition_key == values.get("@household_id")
            and document.get("record_type") == values.get("@record_type")
            and (not statuses or document.get("status") in statuses)
        ]


def inventory(item_id="tissue", name="衛生紙", quantity=1.0, unit="包"):
    return InventoryItem(
        item_id=item_id,
        household_id=HOME,
        name=name,
        quantity=quantity,
        unit=unit,
        created_at=CREATED,
        updated_at=CREATED,
    )


def shopping(shopping_item_id="tissue-mom", item_id="tissue", quantity=2.0, unit="包"):
    return ShoppingItem(
        shopping_item_id=shopping_item_id,
        household_id=HOME,
        name="衛生紙",
        quantity=quantity,
        unit=unit,
        status=ShoppingStatus.PENDING,
        created_at=CREATED,
        updated_at=CREATED,
        item_id=item_id,
    )


def setup(*records):
    container = FakeCosmosContainer()
    for record in records or (inventory(), shopping()):
        container.put(record)
    reader = CosmosReadRepository(container)
    service = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        PurchaseWriteBuilder(reader),
        clock=lambda: T0,
    )
    return service, container, reader


def line(reader, shopping_item_id="tissue-mom", item_id="tissue", quantity=2):
    async def read():
        return (
            await reader.read_shopping_item_with_etag(
                household_id=HOME, shopping_item_id=shopping_item_id
            ),
            await reader.read_inventory_item_with_etag(household_id=HOME, item_id=item_id),
        )

    listed, stocked = asyncio.run(read())
    return purchase_operation(shopping=listed, inventory=stocked, quantity=quantity)


def card(service, *lines):
    presentation = (
        ApprovalPresentation.SINGLE_ACTION if len(lines) == 1 else ApprovalPresentation.BATCH_ACTION
    )
    return asyncio.run(
        service.save(
            service.prepare(household_id=HOME, presentation=presentation, operations=lines)
        )
    )


def confirm(service, record, approved_operation_ids=None):
    return asyncio.run(
        service.resume(
            ResumeRequest(
                household_id=HOME,
                approval_id=record.approval_id,
                approved=True,
                approved_operation_ids=approved_operation_ids,
            )
        )
    )


def test_card_line_holds_what_the_user_sees_and_both_versions():
    service, container, reader = setup()

    operation = line(reader, quantity=2)

    assert operation.action == COMPLETE_PURCHASE
    assert operation.description == "衛生紙 +2 包（從待購清單完成購買）"
    assert {k: v for k, v in operation.values.items() if k != "transaction_id"} == {
        "name": "衛生紙",
        "purchased_quantity": 2,
        "purchased_unit": "包",
        "shopping_item_id": "tissue-mom",
        "item_id": "tissue",
    }
    assert operation.values["transaction_id"]
    assert {(t.record_id, t.etag) for t in operation.targets} == {
        ("shopping_item:tissue-mom", container.get("shopping_item:tissue-mom")["_etag"]),
        ("inventory_item:tissue", container.get("inventory_item:tissue")["_etag"]),
    }


def test_confirmed_purchase_is_one_batch_of_inventory_shopping_transaction_and_event():
    service, container, reader = setup()
    record = card(service, line(reader, quantity=2))
    operation_id = record.operation_ids[0]
    transaction_id = record.operations[0].values["transaction_id"]

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.COMPLETED
    assert len(container.domain_batches) == 1
    assert container.get("inventory_item:tissue")["quantity"] == 3
    assert container.get("shopping_item:tissue-mom")["status"] == "completed"
    transaction = container.get(f"transaction:{transaction_id}")
    assert transaction["quantity_delta"] == 2
    assert transaction["operation_id"] == operation_id
    # No purchase time yet: the Transaction records the confirmation time (Issue #60).
    assert transaction["created_at"] == "2026-11-10T08:00:00.000000Z"
    event = container.get(f"change_event:{operation_id}")
    assert event["approval_id"] == record.approval_id
    assert event["transaction_id"] == transaction_id
    assert result.record.outcome[operation_id]["inventory_quantity"] == 3


def test_two_list_entries_for_one_item_add_to_it_once():
    service, container, reader = setup(
        inventory(),
        shopping("tissue-mom", quantity=2),
        shopping("tissue-dad", quantity=1),
    )
    record = card(
        service, line(reader, "tissue-mom", quantity=2), line(reader, "tissue-dad", quantity=1)
    )

    confirm(service, record, approved_operation_ids=record.operation_ids)

    assert container.get("inventory_item:tissue")["quantity"] == 4
    inventory_writes = [
        op
        for op in container.domain_batches[0]
        if op.document.get("record_type") == "inventory_item"
    ]
    assert len(inventory_writes) == 1  # B1's mapping: one merged shared write (Issue #60)
    assert len(container.of_type("transaction")) == 2


def test_only_ticked_lines_are_written():
    service, container, reader = setup(
        inventory(),
        inventory("milk", "牛奶", 0, "瓶"),
        shopping("tissue-mom"),
        shopping("milk-1", "milk", 1, "瓶"),
    )
    record = card(service, line(reader, "tissue-mom"), line(reader, "milk-1", "milk", 1))

    confirm(service, record, approved_operation_ids=[record.operation_ids[1]])

    assert container.get("inventory_item:tissue")["quantity"] == 1
    assert container.get("shopping_item:tissue-mom")["status"] == "pending"
    assert container.get("inventory_item:milk")["quantity"] == 1


def test_record_changed_after_the_card_writes_nothing_and_asks_for_a_new_card():
    service, container, reader = setup()
    record = card(service, line(reader, quantity=2))
    container.put(inventory(quantity=5))  # someone else updated the tissue count

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    assert container.domain_batches == []
    assert container.get("inventory_item:tissue")["quantity"] == 5


def test_retry_after_a_lost_response_does_not_add_twice():
    service, container, reader = setup()
    record = card(service, line(reader, quantity=2))
    container.lose_next_domain_batch_response = True

    first = confirm(service, record)
    second = confirm(service, record)

    assert first.written and second.written
    assert container.get("inventory_item:tissue")["quantity"] == 3
    assert len(container.domain_batches) == 1


@pytest.mark.parametrize(
    "listed, quantity",
    [
        (shopping(quantity=2), 3),  # more than listed
        (shopping(item_id=None), 1),  # not linked to an inventory item (Issue #90)
        (shopping(unit="串"), 1),  # a different unit
    ],
)
def test_purchases_c1_would_refuse_never_reach_a_card(listed, quantity):
    _, _, reader = setup(inventory(), listed)

    with pytest.raises(ShoppingDecisionRequired):
        line(reader, quantity=quantity)


def test_a_card_with_other_actions_is_not_stored():
    service, container, _ = setup()
    other = NewOperation(action="add_to_shopping_list", description="牛奶", values={})

    with pytest.raises(UnsupportedAction):
        card(service, other)
    assert container.of_type("pending_approval") == []


def test_card_is_checked_against_the_batch_limit_when_saved():
    service, container, reader = setup()

    record = card(service, line(reader))

    # Saving planned the writes once (PR #81) and stored the card without writing data.
    assert container.get(f"pending_approval:{record.approval_id}")["status"] == "pending"
    assert container.domain_batches == []
    assert container.get("inventory_item:tissue")["quantity"] == 1


def test_confirming_twice_at_the_same_time_writes_once():
    service, container, reader = setup()
    record = card(service, line(reader, quantity=2))

    async def both():
        request = ResumeRequest(household_id=HOME, approval_id=record.approval_id, approved=True)
        return await asyncio.gather(service.resume(request), service.resume(request))

    results = asyncio.run(both())

    assert all(result.written for result in results)
    assert container.get("inventory_item:tissue")["quantity"] == 3
    assert len(container.domain_batches) == 1


def test_an_expired_card_is_not_written():
    clock = {"now": T0}
    container = FakeCosmosContainer()
    container.put(inventory())
    container.put(shopping())
    reader = CosmosReadRepository(container)
    service = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        PurchaseWriteBuilder(reader),
        clock=lambda: clock["now"],
    )
    record = card(service, line(reader))
    clock["now"] = T0 + timedelta(minutes=31)

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.EXPIRED
    assert container.get("inventory_item:tissue")["quantity"] == 1
