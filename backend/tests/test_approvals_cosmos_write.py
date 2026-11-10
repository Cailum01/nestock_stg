"""A1 approval service running on B1's `CosmosWriteRepository` (no in-memory port fake).

Only the Cosmos container underneath is faked. Unlike the minimal fake in
`test_cosmos_write.py`, it gives every write a new `_etag`, so a record changed by
someone else is detected the same way Cosmos detects it.

The write builder below stands in for the A1 adapter over C1's purchase plan and
follows B1's mapping in Issue #60: per operation a Transaction create, a Shopping
replace and a change event create; one merged Inventory replace as a shared write.
The main card has two shopping entries (mom and dad each added tissue) that both
add to the same inventory record.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import ChangeEvent
from app.services.approvals import (
    KEEP,
    ApprovalRecord,
    ApprovalService,
    ApprovalStatus,
    BatchTooLarge,
    CancelReason,
    DomainWritePlan,
    NewOperation,
    ResumeOutcome,
    TargetChanged,
    TargetVersion,
)
from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    ReplaceDocument,
    change_event_create,
)

HOME = "home"
SHOPPING = "shopping_item:tissue-mom"
SHOPPING_2 = "shopping_item:tissue-dad"
INVENTORY = "inventory_item:tissue"
PURCHASED_AT = "2026-11-08T04:00:00+00:00"
T0 = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
PENDING_TTL = int((timedelta(minutes=30) + timedelta(hours=24)).total_seconds())
CLOSED_TTL = 24 * 60 * 60


class FakeCosmosContainer:
    """Batch, point-read and query client for one container; each write gets a new ETag."""

    def __init__(self):
        self.documents = {}
        self.batches = []
        self._version = 0
        self.lose_next_domain_batch_response = False
        self.before_next_domain_batch = None

    def put(self, document):
        self._version += 1
        stored = {**document, "_etag": f'"etag-{self._version}"'}
        self.documents[(document["household_id"], document["id"])] = stored
        return stored["_etag"]

    def get(self, document_id, household_id=HOME):
        return self.documents.get((household_id, document_id))

    def of_type(self, record_type):
        return [doc for doc in self.documents.values() if doc.get("record_type") == record_type]

    async def execute_batch(self, *, partition_key, operations):
        await asyncio.sleep(0)
        operations = tuple(operations)
        domain_batch = any(
            op.document.get("record_type") == "operation_receipt" for op in operations
        )
        if domain_batch and self.before_next_domain_batch is not None:
            hook, self.before_next_domain_batch = self.before_next_domain_batch, None
            hook()
        staged = {}
        for index, operation in enumerate(operations):
            document = dict(operation.document)
            if document.get("household_id") != partition_key:
                raise ValueError("Document is outside the batch partition.")
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
        self.batches.append(operations)
        if domain_batch and self.lose_next_domain_batch_response:
            self.lose_next_domain_batch_response = False
            raise TimeoutError("Lost response after the batch committed.")

    async def read_item(self, *, item_id, partition_key):
        await asyncio.sleep(0)
        document = self.documents.get((partition_key, item_id))
        return dict(document) if document is not None else None

    async def query_items(self, *, query, parameters, partition_key):
        """Answers B1's open-approval query the way Cosmos would."""
        await asyncio.sleep(0)
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        statuses = {value for name, value in values.items() if name.endswith("_status")}
        return [
            dict(document)
            for (household, _), document in self.documents.items()
            if household == partition_key == values["@household_id"]
            and document.get("record_type") == values["@record_type"]
            and document.get("status") in statuses
        ]


class PurchaseWriteBuilder:
    """Stands in for the A1 adapter over C1's purchase plan (Issue #60 mapping)."""

    def __init__(self, container):
        self.container = container
        self.extra_shared_writes = 0

    async def build(self, record):
        approved = set(record.approved_operation_ids or ())
        operations_by_id, outcome, added = {}, {}, {}
        inventory = None
        for operation in record.operations:
            if operation.operation_id not in approved:
                continue
            current = {}
            for target in operation.targets:
                document = await self.container.read_item(
                    item_id=target.record_id, partition_key=record.household_id
                )
                if document is None or document["_etag"] != target.etag:
                    raise TargetChanged(target.record_id)
                current[target.record_id] = (document, target.etag)
            values = operation.values
            transaction_id = f"tx-{operation.operation_id}"
            shopping, shopping_etag = current[values["shopping_record_id"]]
            inventory = current[values["inventory_record_id"]]
            added[values["inventory_record_id"]] = (
                added.get(values["inventory_record_id"], 0) + values["quantity"]
            )
            shopping_update = {k: v for k, v in shopping.items() if k != "_etag"}
            shopping_update.update(status="completed", updated_at=record.decided_at.isoformat())
            operations_by_id[operation.operation_id] = [
                CreateDocument(
                    {
                        "id": f"transaction:{transaction_id}",
                        "record_type": "transaction",
                        "household_id": record.household_id,
                        "operation_id": operation.operation_id,
                        "item_id": values["item_id"],
                        "quantity_delta": values["quantity"],
                        "created_at": values["purchased_at"],
                    }
                ),
                ReplaceDocument(shopping_update, if_match_etag=shopping_etag),
                change_event_create(
                    ChangeEvent(
                        event_id=operation.operation_id,
                        household_id=record.household_id,
                        operation_id=operation.operation_id,
                        approval_id=record.approval_id,
                        transaction_id=transaction_id,
                        shopping_item_id=values["shopping_item_id"],
                        item_id=values["item_id"],
                        occurred_at=datetime.fromisoformat(values["purchased_at"]),
                        recorded_at=record.decided_at,
                    )
                ),
            ]
            outcome[operation.operation_id] = {"transaction_id": transaction_id}
        document, etag = inventory
        merged = {k: v for k, v in document.items() if k != "_etag"}
        merged["quantity"] = document["quantity"] + added[document["id"]]
        merged["updated_at"] = record.decided_at.isoformat()
        shared = [ReplaceDocument(merged, if_match_etag=etag)]
        shared += [
            CreateDocument({"id": f"extra:{number}", "household_id": record.household_id})
            for number in range(self.extra_shared_writes)
        ]
        return DomainWritePlan(
            operations_by_id=operations_by_id, shared_operations=shared, outcome=outcome
        )


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


def setup():
    container = FakeCosmosContainer()
    repository = CosmosWriteRepository(container, query_client=container)
    builder = PurchaseWriteBuilder(container)
    clock = Clock()
    service = ApprovalService(repository, builder, clock=clock)
    shopping_etags = [
        container.put(
            {
                "id": shopping_id,
                "record_type": "shopping_item",
                "household_id": HOME,
                "name": "衛生紙",
                "status": "pending",
            }
        )
        for shopping_id in (SHOPPING, SHOPPING_2)
    ]
    inventory_etag = container.put(
        {
            "id": INVENTORY,
            "record_type": "inventory_item",
            "household_id": HOME,
            "name": "衛生紙",
            "quantity": 1,
        }
    )
    return service, container, builder, clock, (shopping_etags, inventory_etag)


def purchase(etags, quantity=1, entry=0):
    shopping_etags, inventory_etag = etags
    shopping_record_id = (SHOPPING, SHOPPING_2)[entry]
    return NewOperation(
        action="complete_purchase",
        description=f"衛生紙買回來 {quantity} 串",
        values={
            "shopping_item_id": shopping_record_id.split(":")[1],
            "item_id": "tissue",
            "shopping_record_id": shopping_record_id,
            "inventory_record_id": INVENTORY,
            "quantity": quantity,
            "purchased_at": PURCHASED_AT,
        },
        targets=[
            TargetVersion(record_id=shopping_record_id, etag=shopping_etags[entry]),
            TargetVersion(record_id=INVENTORY, etag=inventory_etag),
        ],
    )


def make_card(service, etags):
    """Mom's and dad's tissue entries on one card: both add to the same inventory record."""
    return asyncio.run(
        service.save(
            service.prepare(
                household_id=HOME,
                presentation=ApprovalPresentation.BATCH_ACTION,
                operations=[purchase(etags, 1, entry=0), purchase(etags, 2, entry=1)],
            )
        )
    )


def make_single(service, etags):
    return asyncio.run(
        service.save(
            service.prepare(
                household_id=HOME,
                presentation=ApprovalPresentation.SINGLE_ACTION,
                operations=[purchase(etags)],
            )
        )
    )


def confirm(service, record):
    request = ResumeRequest(
        household_id=HOME,
        approval_id=record.approval_id,
        approved=True,
        approved_operation_ids=record.operation_ids,
    )
    return asyncio.run(service.resume(request))


def cancel(service, record):
    request = ResumeRequest(household_id=HOME, approval_id=record.approval_id, approved=False)
    return asyncio.run(service.resume(request))


def card_document(container, record):
    return container.get(f"pending_approval:{record.approval_id}")


def domain_batches(container):
    return [
        batch
        for batch in container.batches
        if any(op.document.get("record_type") == "operation_receipt" for op in batch)
    ]


# ----- 1. saving a card --------------------------------------------------------------------


def test_save_stores_a_pending_card_through_b1():
    service, container, _, _, etags = setup()

    record = make_card(service, etags)

    document = card_document(container, record)
    assert document["status"] == "pending"
    assert document["ttl"] == PENDING_TTL
    assert ApprovalRecord.from_document(document) == record


# ----- 2. saving the same card again ------------------------------------------------------


def test_saving_the_same_card_again_keeps_one_card():
    service, container, _, _, etags = setup()
    prepared = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[purchase(etags)],
    )

    first = asyncio.run(service.save(prepared))
    second = asyncio.run(service.save(prepared))

    assert first == second
    assert len(container.of_type("pending_approval")) == 1


# ----- 3. open cards ---------------------------------------------------------------------


def test_open_cards_come_from_b1_query_and_expired_ones_are_recorded():
    service, container, _, clock, etags = setup()
    old = make_single(service, etags)
    clock.advance(31)
    cancelled = make_single(service, etags)
    cancel(service, cancelled)
    current = make_single(service, etags)

    open_cards = asyncio.run(service.list_open(household_id=HOME))

    assert [card.approval_id for card in open_cards] == [current.approval_id]
    assert card_document(container, old)["status"] == "expired"
    assert card_document(container, old)["ttl"] == CLOSED_TTL


# ----- 4. confirm writes everything in one batch -----------------------------------------


def test_confirm_writes_purchase_receipts_events_and_completion_in_one_b1_batch():
    service, container, _, clock, etags = setup()
    record = make_card(service, etags)
    clock.advance(5)

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.COMPLETED
    (batch,) = domain_batches(container)
    # 2 receipts + 2 x (transaction, shopping, change event) + merged inventory + card.
    assert len(batch) == 10
    assert batch[-2].document["id"] == INVENTORY
    assert batch[-1].document["status"] == "completed"
    card = card_document(container, record)
    assert card["status"] == "completed"
    assert card["ttl"] == KEEP
    decided_at = datetime.fromisoformat(card["decided_at"])
    assert decided_at == T0 + timedelta(minutes=5)
    assert container.get(INVENTORY)["quantity"] == 1 + 1 + 2
    assert datetime.fromisoformat(container.get(INVENTORY)["updated_at"]) == decided_at
    assert container.get(SHOPPING)["status"] == "completed"
    assert container.get(SHOPPING_2)["status"] == "completed"
    assert len(container.of_type("transaction")) == 2
    assert len(container.of_type("operation_receipt")) == 2
    events = container.of_type("change_event")
    assert {event["approval_id"] for event in events} == {record.approval_id}
    assert {event["event_id"] for event in events} == set(record.operation_ids)
    assert all(datetime.fromisoformat(event["recorded_at"]) == decided_at for event in events)
    assert all(
        datetime.fromisoformat(event["occurred_at"]) == datetime.fromisoformat(PURCHASED_AT)
        for event in events
    )


# ----- 5. lost response after the batch committed ----------------------------------------


def test_lost_response_after_commit_is_written_once():
    service, container, _, _, etags = setup()
    record = make_card(service, etags)
    container.lose_next_domain_batch_response = True

    first = confirm(service, record)
    again = confirm(service, record)

    assert first.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert again.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert len(domain_batches(container)) == 1
    assert len(container.of_type("transaction")) == 2
    assert container.get(INVENTORY)["quantity"] == 4


# ----- 6. cancel -------------------------------------------------------------------------


def test_cancel_through_b1_writes_nothing():
    service, container, _, _, etags = setup()
    record = make_card(service, etags)

    result = cancel(service, record)

    assert result.outcome is ResumeOutcome.CANCELLED
    card = card_document(container, record)
    assert card["status"] == "cancelled"
    assert card["ttl"] == CLOSED_TTL
    assert domain_batches(container) == []
    assert container.get(INVENTORY)["quantity"] == 1


# ----- 7. expiry -------------------------------------------------------------------------


def test_confirm_after_expiry_is_recorded_through_b1():
    service, container, _, clock, etags = setup()
    record = make_card(service, etags)
    clock.advance(31)

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.EXPIRED
    assert card_document(container, record)["status"] == "expired"
    assert domain_batches(container) == []


# ----- 8. someone else changed a record ---------------------------------------------------


def test_record_changed_by_someone_else_during_the_write_needs_a_new_card():
    service, container, _, _, etags = setup()
    record = make_card(service, etags)

    def someone_else_updates_inventory():
        document = {k: v for k, v in container.get(INVENTORY).items() if k != "_etag"}
        container.put({**document, "quantity": 9})

    container.before_next_domain_batch = someone_else_updates_inventory

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    card = card_document(container, record)
    assert card["status"] == "cancelled"
    assert card["cancel_reason"] == CancelReason.REPLACED
    assert container.get(INVENTORY)["quantity"] == 9
    assert container.of_type("transaction") == []
    assert container.of_type("operation_receipt") == []


def test_record_changed_before_the_card_is_confirmed_needs_a_new_card():
    service, container, _, _, etags = setup()
    record = make_card(service, etags)
    document = {k: v for k, v in container.get(SHOPPING).items() if k != "_etag"}
    container.put({**document, "name": "衛生紙（改）"})

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    assert card_document(container, record)["status"] == "cancelled"
    assert domain_batches(container) == []


# ----- batch limit includes shared writes ---------------------------------------------------


@pytest.mark.parametrize(("extra", "fits"), [(90, True), (91, False)])
def test_shared_writes_count_toward_the_batch_limit(extra, fits):
    """2 receipts + 6 per-operation writes + merged inventory + extras + card <= 100."""
    service, container, builder, _, etags = setup()
    record = make_card(service, etags)
    builder.extra_shared_writes = extra

    if fits:
        assert confirm(service, record).outcome is ResumeOutcome.COMPLETED
        assert len(domain_batches(container)[0]) == 100
    else:
        with pytest.raises(BatchTooLarge):
            confirm(service, record)
        assert card_document(container, record)["status"] == ApprovalStatus.PENDING
        assert domain_batches(container) == []


@pytest.mark.parametrize(("extra", "fits"), [(90, True), (91, False)])
def test_card_over_the_batch_limit_is_never_stored_or_listed(extra, fits):
    """Checked when the card is saved, before the user can see it (Issue #60)."""
    service, container, builder, _, etags = setup()
    builder.extra_shared_writes = extra
    prepared = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.BATCH_ACTION,
        operations=[purchase(etags, 1, entry=0), purchase(etags, 2, entry=1)],
    )

    if fits:
        asyncio.run(service.save(prepared))
        listed = asyncio.run(service.list_open(household_id=HOME))
        assert [card.approval_id for card in listed] == [prepared.approval_id]
    else:
        with pytest.raises(BatchTooLarge):
            asyncio.run(service.save(prepared))
        assert card_document(container, prepared) is None
        assert asyncio.run(service.list_open(household_id=HOME)) == []
        assert container.batches == []
