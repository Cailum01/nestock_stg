"""A1 approval service against PR #47 (contracts/approval-lifecycle.md).

The fake storage follows B1's PR #51 batch semantics: receipts first, then the
per-operation writes, then shared writes; any failure writes nothing.
"""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.services.approvals import (
    KEEP,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalService,
    ApprovalStatus,
    BatchTooLarge,
    CancelReason,
    DomainWritePlan,
    InvalidSelection,
    NewOperation,
    NewOption,
    ResumeOutcome,
    TargetChanged,
    TargetVersion,
)
from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    ReplaceDocument,
    WriteStatus,
)

HOME = "home"
RICE = "inventory_item:rice"
T0 = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)
DAY = 24 * 60 * 60


class ApprovalIdConflict(Exception):
    """Stands in for PR #54's error: same approval_id, different content."""


class Stored:
    def __init__(self, document, etag):
        self.document = document
        self.etag = etag


class FakeStorage:
    def __init__(self):
        self.docs = {}
        self.receipts = {}
        self.batches = []
        self.batch_attempts = 0
        self.create_attempts = 0
        self.lose_next_create_response = False
        self._counter = 0
        self.fail_next_batch = None
        self.lose_next_batch_response = False
        self.fail_next_replace = None
        self.fail_next_receipt_read = None
        self.before_batch = None
        self.before_replace = None

    def _etag(self):
        self._counter += 1
        return f"etag-{self._counter}"

    def put(self, document):
        stored = dict(document)
        stored["_etag"] = self._etag()
        self.docs[(stored["household_id"], stored["id"])] = stored
        return stored["_etag"]

    def card(self, approval_id, household_id=HOME):
        return self.docs[(household_id, f"pending_approval:{approval_id}")]

    async def create_pending_approval(self, *, household_id, approval, ttl_seconds):
        """Follows PR #54: the same card again returns it; different content is refused."""
        await asyncio.sleep(0)
        assert approval["household_id"] == household_id and approval["status"] == "pending"
        assert isinstance(ttl_seconds, int) and ttl_seconds > 0
        self.create_attempts += 1
        document = dict(approval)
        document["ttl"] = ttl_seconds
        current = self.docs.get((household_id, approval["id"]))
        if current is not None:
            if {k: v for k, v in current.items() if k != "_etag"} != document:
                raise ApprovalIdConflict(approval["approval_id"])
        else:
            self.put(document)
        if self.lose_next_create_response:
            self.lose_next_create_response = False
            raise TimeoutError("Lost response after the card was stored.")
        return await self.read_pending_approval(
            household_id=household_id, approval_id=approval["approval_id"]
        )

    async def read_pending_approval(self, *, household_id, approval_id):
        await asyncio.sleep(0)
        document = self.docs.get((household_id, f"pending_approval:{approval_id}"))
        if document is None:
            return None
        return Stored(dict(document), document["_etag"])

    async def replace_pending_approval(self, *, household_id, approval_id, document, if_match_etag):
        """Follows PR #54: approval-only transitions, never `completed`."""
        await asyncio.sleep(0)
        assert document["approval_id"] == approval_id
        if self.before_replace is not None:
            hook, self.before_replace = self.before_replace, None
            await hook()
        if self.fail_next_replace is not None:
            error, self.fail_next_replace = self.fail_next_replace, None
            raise error
        current = self.docs.get((household_id, document["id"]))
        if current is None or current["_etag"] != if_match_etag:
            raise BatchOperationConflict(0)
        allowed = {
            "pending": {"confirmed", "cancelled", "expired"},
            "confirmed": {"cancelled", "expired"},
        }
        if document["status"] not in allowed.get(current["status"], set()):
            raise ValueError(f"{current['status']} -> {document['status']} is not allowed.")
        self.put(document)
        return await self.read_pending_approval(household_id=household_id, approval_id=approval_id)

    async def read_operation_receipt(self, *, household_id, operation_id):
        await asyncio.sleep(0)
        if self.fail_next_receipt_read is not None:
            error, self.fail_next_receipt_read = self.fail_next_receipt_read, None
            raise error
        return self.receipts.get((household_id, operation_id))

    async def list_open_approvals(self, *, household_id):
        await asyncio.sleep(0)
        return [
            Stored(dict(document), document["_etag"])
            for (household, _), document in self.docs.items()
            if household == household_id
            and document.get("record_type") == "pending_approval"
            and document["status"] in ("pending", "confirmed")
        ]

    async def execute_idempotent_batches(
        self, *, household_id, operations_by_id, shared_operations=()
    ):
        await asyncio.sleep(0)
        self.batch_attempts += 1
        if self.before_batch is not None:
            hook, self.before_batch = self.before_batch, None
            await hook()
        if self.fail_next_batch is not None:
            error, self.fail_next_batch = self.fail_next_batch, None
            raise error
        ordered = [("receipt", operation_id) for operation_id in sorted(operations_by_id)]
        for operation_id in sorted(operations_by_id):
            ordered.extend(operations_by_id[operation_id])
        ordered.extend(shared_operations)

        staged_docs, staged_receipts = {}, {}
        for index, item in enumerate(ordered):
            if isinstance(item, tuple):
                if (household_id, item[1]) in self.receipts:
                    present = [op for op in operations_by_id if (household_id, op) in self.receipts]
                    if len(present) == len(operations_by_id):
                        return WriteStatus.ALREADY_APPLIED
                    raise RuntimeError("Only part of the receipts exists.")
                staged_receipts[(household_id, item[1])] = {"operation_id": item[1]}
                continue
            document = dict(item.document)
            key = (household_id, document["id"])
            current = staged_docs.get(key) or self.docs.get(key)
            if isinstance(item, CreateDocument) and current is not None:
                raise BatchOperationConflict(index)
            if isinstance(item, ReplaceDocument) and (
                current is None or current["_etag"] != item.if_match_etag
            ):
                raise BatchOperationConflict(index)
            self._counter += 1
            document["_etag"] = f"etag-{self._counter}"
            staged_docs[key] = document

        self.docs.update(staged_docs)
        self.receipts.update(staged_receipts)
        self.batches.append({"operations_by_id": dict(operations_by_id), "ordered": ordered})
        if self.lose_next_batch_response:
            self.lose_next_batch_response = False
            raise TimeoutError("Lost response after the batch committed.")
        return WriteStatus.APPLIED


class FakeWriteBuilder:
    """Stands in for C1/B1: a transaction per operation plus inventory updates."""

    def __init__(self, storage):
        self.storage = storage
        self.extra_writes = 0
        self.extra_writes_by_option = {}
        """More writes when a given purchase option is chosen."""

    async def build(self, record):
        await asyncio.sleep(0)
        operations_by_id, outcome = {}, {}
        for operation in record.operations:
            if operation.operation_id not in (record.approved_operation_ids or ()):
                continue
            writes = [
                CreateDocument(
                    {
                        "id": f"transaction:{operation.operation_id}",
                        "record_type": "transaction",
                        "household_id": record.household_id,
                        "operation_id": operation.operation_id,
                    }
                )
            ]
            for target in operation.targets:
                current = self.storage.docs.get((record.household_id, target.record_id))
                if current is None or current["_etag"] != target.etag:
                    raise TargetChanged(target.record_id)
                updated = {k: v for k, v in current.items() if k != "_etag"}
                updated["quantity"] = current["quantity"] + operation.values.get("delta", 0)
                writes.append(ReplaceDocument(updated, if_match_etag=target.etag))
            extra = self.extra_writes + self.extra_writes_by_option.get(
                record.selected_option_id, 0
            )
            for number in range(extra):
                writes.append(
                    CreateDocument(
                        {
                            "id": f"extra:{operation.operation_id}:{number}",
                            "household_id": record.household_id,
                        }
                    )
                )
            operations_by_id[operation.operation_id] = writes
            outcome[operation.operation_id] = {"option": record.selected_option_id}
        return DomainWritePlan(operations_by_id=operations_by_id, outcome=outcome)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


def setup(lifetime=timedelta(minutes=30)):
    storage = FakeStorage()
    builder = FakeWriteBuilder(storage)
    clock = Clock()
    service = ApprovalService(storage, builder, lifetime=lifetime, clock=clock)
    rice_etag = storage.put({"id": RICE, "household_id": HOME, "item_id": "rice", "quantity": 2})
    return service, storage, builder, clock, rice_etag


def rice_operation(etag, delta=6):
    return NewOperation(
        action="add_stock",
        description=f"白米入庫 {delta} 包",
        values={"delta": delta},
        targets=[TargetVersion(record_id=RICE, etag=etag)],
    )


def plain_operation(name):
    return NewOperation(action="add_to_shopping_list", description=name, values={"name": name})


def create(service, **card):
    return asyncio.run(service.save(service.prepare(household_id=HOME, **card)))


def make_single(service, etag):
    return create(
        service,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[rice_operation(etag)],
    )


def make_batch(service, names=("豆漿", "衛生紙", "洗衣精")):
    return create(
        service,
        presentation=ApprovalPresentation.BATCH_ACTION,
        operations=[plain_operation(name) for name in names],
    )


def make_purchase(service):
    return create(
        service,
        presentation=ApprovalPresentation.PURCHASE_OPTIONS,
        operations=[plain_operation("衛生紙")],
        options=[
            NewOption(option_id="option_001", values={"store": "店家 A", "price": 99}),
            NewOption(option_id="option_002", values={"store": "店家 B", "price": 109}),
        ],
    )


def confirm(service, record, **selection):
    request = ResumeRequest(
        household_id=HOME, approval_id=record.approval_id, approved=True, **selection
    )
    return asyncio.run(service.resume(request))


def cancel(service, record, **selection):
    request = ResumeRequest(
        household_id=HOME, approval_id=record.approval_id, approved=False, **selection
    )
    return asyncio.run(service.resume(request))


def cards(storage):
    return [key[1] for key in storage.docs if key[1].startswith("pending_approval:")]


def stored_record(storage, record):
    return ApprovalRecord.from_document(storage.card(record.approval_id))


# ----- creating and showing cards ------------------------------------------------------


def test_create_stores_pending_card_with_server_ids_and_ttl():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)

    document = storage.card(record.approval_id)
    assert len(record.approval_id) == 32 and len(record.operation_ids[0]) == 32
    assert record.approval_id != record.operation_ids[0]
    assert document["id"] == f"pending_approval:{record.approval_id}"
    assert document["record_type"] == "pending_approval"
    assert document["status"] == "pending"
    assert record.expires_at == T0 + timedelta(minutes=30)
    assert document["ttl"] == 30 * 60 + DAY


def test_prepare_stores_nothing_until_save():
    service, storage, _, _, etag = setup()
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[rice_operation(etag)],
    )
    assert not cards(storage)

    saved = asyncio.run(service.save(record))

    assert saved == record
    assert cards(storage) == [f"pending_approval:{record.approval_id}"]


def test_retrying_save_after_a_lost_response_keeps_one_card():
    service, storage, _, _, etag = setup()
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[rice_operation(etag)],
    )
    storage.lose_next_create_response = True
    with pytest.raises(TimeoutError):
        asyncio.run(service.save(record))  # stored, but the reply was lost

    saved = asyncio.run(service.save(record))  # the caller retries with the same card

    assert saved == record
    assert storage.create_attempts == 2
    assert cards(storage) == [f"pending_approval:{record.approval_id}"]
    assert confirm(service, saved).outcome is ResumeOutcome.COMPLETED
    assert storage.docs[(HOME, RICE)]["quantity"] == 8  # added once


def test_save_refuses_different_content_under_the_same_id():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    before = dict(storage.card(record.approval_id))
    changed = record.model_copy(
        update={"operations": [record.operations[0].model_copy(update={"values": {"delta": 9}})]}
    )

    with pytest.raises(ApprovalIdConflict):
        asyncio.run(service.save(changed))
    assert storage.card(record.approval_id) == before


def test_save_takes_only_pending_cards():
    service, storage, _, _, etag = setup()
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[rice_operation(etag)],
    )

    with pytest.raises(ValueError):
        asyncio.run(service.save(record.model_copy(update={"status": ApprovalStatus.CONFIRMED})))
    assert not cards(storage)


def test_lifetime_is_configurable():
    service, storage, _, _, etag = setup(lifetime=timedelta(minutes=5))
    record = make_single(service, etag)

    assert record.expires_at == T0 + timedelta(minutes=5)
    assert storage.card(record.approval_id)["ttl"] == 5 * 60 + DAY


@pytest.mark.parametrize(
    ("presentation", "operation_count", "options"),
    [
        (ApprovalPresentation.SINGLE_ACTION, 2, []),
        (ApprovalPresentation.BATCH_ACTION, 0, []),
        (ApprovalPresentation.BATCH_ACTION, 1, [NewOption(option_id="o1", values={})]),
        (ApprovalPresentation.PURCHASE_OPTIONS, 2, [NewOption(option_id="o1", values={})]),
        (ApprovalPresentation.PURCHASE_OPTIONS, 1, []),
        # Issue #90 format only: no confirm rules yet.
        (
            ApprovalPresentation.ITEM_MATCH,
            1,
            [
                NewOption(option_id="same_item", values={"label": "是"}),
                NewOption(option_id="new_item", values={"label": "不是"}),
            ],
        ),
        (
            ApprovalPresentation.PURCHASE_OPTIONS,
            1,
            [NewOption(option_id="o1", values={}), NewOption(option_id="o1", values={})],
        ),
    ],
)
def test_prepare_rejects_card_shapes_outside_v8(presentation, operation_count, options):
    service, storage, _, _, _ = setup()
    operations = [plain_operation(f"item {n}") for n in range(operation_count)]

    with pytest.raises(ValueError):
        service.prepare(
            household_id=HOME,
            presentation=presentation,
            operations=operations,
            options=options,
        )
    assert not cards(storage)


def test_option_values_may_not_carry_their_own_option_id():
    service, storage, _, _, _ = setup()

    with pytest.raises(ValueError, match="option_id"):
        service.prepare(
            household_id=HOME,
            presentation=ApprovalPresentation.PURCHASE_OPTIONS,
            operations=[plain_operation("衛生紙")],
            options=[NewOption(option_id="o1", values={"option_id": "momo-123"})],
        )
    assert not cards(storage)


def test_public_card_shows_operations_and_purchase_options():
    service, _, _, _, etag = setup()
    record = make_purchase(service)

    card = ApprovalService.to_pending_approval(record)

    assert card.approval_id == record.approval_id
    assert card.presentation is ApprovalPresentation.PURCHASE_OPTIONS
    assert card.operations[0].operation_id == record.operation_ids[0]
    # Issue #90: the App picks how to show an operation by its action.
    assert card.operations[0].action == record.operations[0].action
    # v8 section 12: options sit on the card, not inside the operation's values.
    assert [option["option_id"] for option in card.options] == ["option_001", "option_002"]
    assert "options" not in card.operations[0].values
    assert ApprovalService.to_pending_approval(make_single(service, etag)).options == []


def test_public_card_always_shows_the_cards_own_option_id():
    service, storage, _, _, _ = setup()
    record = make_purchase(service)
    document = dict(storage.card(record.approval_id))
    document["options"][0]["values"]["option_id"] = "momo-123"  # written by other code

    card = ApprovalService.to_pending_approval(ApprovalRecord.from_document(document))

    assert [option["option_id"] for option in card.options] == ["option_001", "option_002"]


def test_document_round_trip_ignores_cosmos_system_fields():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    document = dict(storage.card(record.approval_id))
    document.update({"_rid": "x", "_ts": 1, "_self": "y", "_attachments": "z"})

    assert ApprovalRecord.from_document(document) == record


# ----- confirm and write ---------------------------------------------------------------


def test_confirm_writes_data_receipts_and_completion_in_one_batch():
    service, storage, _, clock, etag = setup()
    record = make_single(service, etag)
    clock.advance(1)

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.COMPLETED and result.written
    card = stored_record(storage, record)
    assert card.status is ApprovalStatus.COMPLETED
    assert card.decided_at == T0 + timedelta(minutes=1)
    assert card.completed_at is not None
    assert card.approved_operation_ids == record.operation_ids
    assert card.outcome == {record.operation_ids[0]: {"option": None}}
    assert storage.card(record.approval_id)["ttl"] == KEEP
    assert storage.docs[(HOME, RICE)]["quantity"] == 8
    assert (HOME, record.operation_ids[0]) in storage.receipts
    assert len(storage.batches) == 1
    last = storage.batches[0]["ordered"][-1]
    assert isinstance(last, ReplaceDocument) and last.document["status"] == "completed"


def test_confirming_twice_writes_once():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)

    first = confirm(service, record)
    second = confirm(service, record)

    assert first.outcome is ResumeOutcome.COMPLETED
    assert second.outcome is ResumeOutcome.ALREADY_COMPLETED and second.written
    assert second.record.outcome == first.record.outcome
    assert len(storage.batches) == 1
    assert storage.docs[(HOME, RICE)]["quantity"] == 8


def test_two_confirms_at_the_same_time_write_once():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    request = ResumeRequest(household_id=HOME, approval_id=record.approval_id, approved=True)

    async def both():
        return await asyncio.gather(service.resume(request), service.resume(request))

    results = asyncio.run(both())

    assert all(result.written for result in results)
    assert ResumeOutcome.COMPLETED in {result.outcome for result in results}
    assert len(storage.batches) == 1
    assert storage.docs[(HOME, RICE)]["quantity"] == 8


def test_other_request_finishing_first_is_reported_as_already_completed():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    request = ResumeRequest(household_id=HOME, approval_id=record.approval_id, approved=True)

    async def other_request_finishes_first():
        await service.resume(request)

    storage.before_batch = other_request_finishes_first
    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.ALREADY_COMPLETED and result.written
    assert result.record == stored_record(storage, record)
    assert len(storage.batches) == 1
    assert storage.docs[(HOME, RICE)]["quantity"] == 8


def test_lost_response_after_commit_is_reported_as_written_once():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    storage.lose_next_batch_response = True

    result = confirm(service, record)
    retry = confirm(service, record)

    assert result.written and retry.written
    assert len(storage.batches) == 1
    assert storage.docs[(HOME, RICE)]["quantity"] == 8


def test_failed_batch_keeps_card_confirmed_and_retry_reuses_ids():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")

    failed = confirm(service, record)

    assert failed.outcome is ResumeOutcome.NOT_WRITTEN and not failed.written
    assert stored_record(storage, record).status is ApprovalStatus.CONFIRMED
    assert storage.card(record.approval_id)["ttl"] == KEEP
    assert not storage.receipts

    retry = confirm(service, record)

    assert retry.outcome is ResumeOutcome.COMPLETED
    assert list(storage.batches[0]["operations_by_id"]) == record.operation_ids


def test_batch_writes_only_the_approved_operations():
    service, storage, _, _, _ = setup()
    record = make_batch(service)
    kept = [record.operation_ids[0], record.operation_ids[2]]

    result = confirm(service, record, approved_operation_ids=kept)

    assert result.outcome is ResumeOutcome.COMPLETED
    assert set(storage.batches[0]["operations_by_id"]) == set(kept)
    assert {operation_id for _, operation_id in storage.receipts} == set(kept)


def test_retry_with_a_different_selection_is_rejected():
    service, storage, _, _, _ = setup()
    record = make_batch(service)
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, record, approved_operation_ids=[record.operation_ids[0]])

    with pytest.raises(InvalidSelection):
        confirm(service, record, approved_operation_ids=record.operation_ids[:2])

    assert stored_record(storage, record).approved_operation_ids == [record.operation_ids[0]]
    assert not storage.batches


@pytest.mark.parametrize(
    ("kind", "selection"),
    [
        ("single", {"approved_operation_ids": ["anything"]}),
        ("single", {"selected_option_id": "option_001"}),
        ("batch", {}),
        ("batch", {"approved_operation_ids": ["not-on-card"]}),
        ("batch", {"selected_option_id": "option_001"}),
        ("purchase", {}),
        ("purchase", {"selected_option_id": "option_999"}),
        ("purchase", {"approved_operation_ids": ["anything"]}),
    ],
)
def test_selection_must_fit_the_card_type(kind, selection):
    service, storage, _, _, etag = setup()
    record = {
        "single": lambda: make_single(service, etag),
        "batch": lambda: make_batch(service),
        "purchase": lambda: make_purchase(service),
    }[kind]()

    with pytest.raises(InvalidSelection):
        confirm(service, record, **selection)

    assert stored_record(storage, record).status is ApprovalStatus.PENDING
    assert not storage.batches


def test_purchase_card_stores_the_chosen_option():
    service, storage, _, _, _ = setup()
    record = make_purchase(service)

    result = confirm(service, record, selected_option_id="option_002")

    assert result.outcome is ResumeOutcome.COMPLETED
    assert stored_record(storage, record).selected_option_id == "option_002"
    assert result.record.outcome == {record.operation_ids[0]: {"option": "option_002"}}


def test_batch_too_large_is_rejected_before_any_write():
    """The card fitted when it was saved; the writes grew before the user confirmed."""
    service, storage, builder, _, etag = setup()
    record = make_single(service, etag)
    builder.extra_writes = 98  # receipt + transaction + inventory + 98 + approval = 102

    with pytest.raises(BatchTooLarge):
        confirm(service, record)

    assert stored_record(storage, record).status is ApprovalStatus.PENDING
    assert not storage.batches


# ----- batch limit when the card is made (Issue #60) ----------------------------------------


@pytest.mark.parametrize(("extra", "fits"), [(96, True), (97, False)])
def test_card_over_the_batch_limit_is_never_stored_or_shown(extra, fits):
    """receipt + transaction + inventory + extras + completed card must be <= 100."""
    service, storage, builder, _, etag = setup()
    builder.extra_writes = extra
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[rice_operation(etag)],
    )

    if fits:
        asyncio.run(service.save(record))
        assert [card.approval_id for card in asyncio.run(service.list_open(household_id=HOME))] == [
            record.approval_id
        ]
        assert confirm(service, record).outcome is ResumeOutcome.COMPLETED
        assert len(storage.batches[0]["ordered"]) == 100  # the completed card included
    else:
        with pytest.raises(BatchTooLarge):
            asyncio.run(service.save(record))
        assert cards(storage) == []
        assert storage.create_attempts == 0
        assert asyncio.run(service.list_open(household_id=HOME)) == []


def test_batch_card_is_checked_with_every_operation_approved():
    """The user may approve all items, so the full card has to fit."""
    service, storage, builder, _, _ = setup()
    builder.extra_writes = 31  # 3 x (receipt + transaction + 31) + card = 100
    fits = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.BATCH_ACTION,
        operations=[plain_operation(name) for name in ("豆漿", "衛生紙", "洗衣精")],
    )
    asyncio.run(service.save(fits))

    builder.extra_writes = 32  # 3 x 34 + card = 103, although one item alone would fit
    too_large = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.BATCH_ACTION,
        operations=[plain_operation(name) for name in ("豆漿", "衛生紙", "洗衣精")],
    )
    with pytest.raises(BatchTooLarge):
        asyncio.run(service.save(too_large))

    assert cards(storage) == [f"pending_approval:{fits.approval_id}"]


def test_purchase_card_is_checked_with_every_option():
    """option_001 fits; option_002 does not, so the card is not stored."""
    service, storage, builder, _, _ = setup()
    builder.extra_writes_by_option = {"option_002": 98}  # receipt + 1 + 98 + card = 101
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.PURCHASE_OPTIONS,
        operations=[plain_operation("衛生紙")],
        options=[
            NewOption(option_id="option_001", values={"store": "店家 A", "price": 99}),
            NewOption(option_id="option_002", values={"store": "店家 B", "price": 109}),
        ],
    )

    with pytest.raises(BatchTooLarge):
        asyncio.run(service.save(record))

    assert cards(storage) == []


def test_card_whose_record_changed_before_saving_is_not_stored():
    service, storage, _, _, etag = setup()
    record = service.prepare(
        household_id=HOME,
        presentation=ApprovalPresentation.SINGLE_ACTION,
        operations=[rice_operation(etag)],
    )
    storage.put({"id": RICE, "household_id": HOME, "item_id": "rice", "quantity": 5})

    with pytest.raises(TargetChanged):
        asyncio.run(service.save(record))

    assert cards(storage) == []


def test_reused_operation_id_with_different_values_needs_a_new_card():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    storage.fail_next_batch = OperationIdConflict("different canonical values")

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    card = stored_record(storage, record)
    assert card.status is ApprovalStatus.CANCELLED
    assert card.cancel_reason is CancelReason.REPLACED


# ----- cancel --------------------------------------------------------------------------


def test_cancel_writes_nothing_and_ignores_selection():
    service, storage, _, clock, _ = setup()
    record = make_batch(service)
    clock.advance(2)

    result = cancel(service, record, approved_operation_ids=["ignored"])

    assert result.outcome is ResumeOutcome.CANCELLED and not result.written
    card = stored_record(storage, record)
    assert card.status is ApprovalStatus.CANCELLED
    assert card.cancel_reason is CancelReason.USER
    assert card.decided_at == T0 + timedelta(minutes=2)
    assert storage.card(record.approval_id)["ttl"] == DAY
    assert not storage.batches


def test_confirmed_card_can_be_cancelled_and_then_stays_cancelled():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, record)

    cancelled = cancel(service, record)
    again = confirm(service, record)

    assert cancelled.outcome is ResumeOutcome.CANCELLED
    assert again.outcome is ResumeOutcome.CANCELLED
    assert not storage.batches
    assert storage.docs[(HOME, RICE)]["quantity"] == 2


def test_cancel_during_the_write_wins_and_nothing_is_written():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)

    async def user_cancels():
        await service.resume(
            ResumeRequest(household_id=HOME, approval_id=record.approval_id, approved=False)
        )

    storage.before_batch = user_cancels
    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.CANCELLED
    assert not storage.batches and not storage.receipts
    assert storage.docs[(HOME, RICE)]["quantity"] == 2


def test_cancel_that_loses_to_a_confirm_still_cancels():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)

    async def confirm_arrives_first():
        document = dict(storage.card(record.approval_id))
        document.update({"status": "confirmed", "approved_operation_ids": record.operation_ids})
        storage.put(document)

    storage.before_replace = confirm_arrives_first
    result = cancel(service, record)

    assert result.outcome is ResumeOutcome.CANCELLED
    assert stored_record(storage, record).status is ApprovalStatus.CANCELLED
    assert not storage.batches


def test_cancel_after_completion_keeps_the_write():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    confirm(service, record)

    result = cancel(service, record)

    assert result.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert stored_record(storage, record).status is ApprovalStatus.COMPLETED
    assert storage.docs[(HOME, RICE)]["quantity"] == 8


def test_agent_replacing_a_card_cancels_the_old_one():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)

    result = asyncio.run(service.cancel_replaced(household_id=HOME, approval_id=record.approval_id))

    assert result.outcome is ResumeOutcome.CANCELLED
    card = stored_record(storage, record)
    assert card.cancel_reason is CancelReason.REPLACED
    assert card.decided_at is None


# ----- expiry --------------------------------------------------------------------------


def test_confirm_after_expiry_is_rejected_and_recorded():
    service, storage, _, clock, etag = setup()
    record = make_single(service, etag)
    clock.advance(31)

    result = confirm(service, record)
    again = confirm(service, record)

    assert result.outcome is ResumeOutcome.EXPIRED
    assert again.outcome is ResumeOutcome.EXPIRED
    assert stored_record(storage, record).status is ApprovalStatus.EXPIRED
    assert storage.card(record.approval_id)["ttl"] == DAY
    assert not storage.batches


def test_expiry_is_decided_even_when_recording_it_fails():
    service, storage, _, clock, etag = setup()
    record = make_single(service, etag)
    clock.advance(31)
    storage.fail_next_replace = RuntimeError("Cosmos unavailable")

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.EXPIRED
    assert stored_record(storage, record).status is ApprovalStatus.PENDING
    assert not storage.batches


def test_confirmed_card_can_be_retried_after_expiry():
    service, storage, _, clock, etag = setup()
    record = make_single(service, etag)
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, record)
    clock.advance(60)

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.COMPLETED
    assert storage.docs[(HOME, RICE)]["quantity"] == 8


def test_purchase_card_cannot_be_retried_after_expiry():
    service, storage, _, clock, _ = setup()
    record = make_purchase(service)
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, record, selected_option_id="option_001")
    clock.advance(31)

    result = confirm(service, record, selected_option_id="option_001")

    assert result.outcome is ResumeOutcome.EXPIRED
    assert stored_record(storage, record).status is ApprovalStatus.EXPIRED
    assert not storage.batches


# ----- changed data and recovery --------------------------------------------------------


def test_changed_record_before_the_write_cancels_the_card():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    storage.put({"id": RICE, "household_id": HOME, "item_id": "rice", "quantity": 1})

    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD and not result.written
    card = stored_record(storage, record)
    assert card.status is ApprovalStatus.CANCELLED
    assert card.cancel_reason is CancelReason.REPLACED
    assert not storage.batches


def test_changed_record_during_the_write_cancels_the_card():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)

    async def someone_else_writes():
        storage.put({"id": RICE, "household_id": HOME, "item_id": "rice", "quantity": 1})

    storage.before_batch = someone_else_writes
    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.NEEDS_NEW_CARD
    assert stored_record(storage, record).cancel_reason is CancelReason.REPLACED
    assert not storage.receipts
    assert storage.docs[(HOME, RICE)]["quantity"] == 1


def test_card_version_change_during_the_write_keeps_the_card():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)

    async def card_rewritten_unchanged():
        storage.put(dict(storage.card(record.approval_id)))  # same content, new ETag

    storage.before_batch = card_rewritten_unchanged
    result = confirm(service, record)

    assert result.outcome is ResumeOutcome.NOT_WRITTEN
    assert stored_record(storage, record).status is ApprovalStatus.CONFIRMED
    assert storage.docs[(HOME, RICE)]["quantity"] == 2

    retry = confirm(service, record)

    assert retry.outcome is ResumeOutcome.COMPLETED


def test_partial_receipts_stop_writing():
    service, storage, _, _, _ = setup()
    record = make_batch(service, names=("豆漿", "衛生紙"))
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, record, approved_operation_ids=record.operation_ids)
    storage.receipts[(HOME, record.operation_ids[0])] = {"operation_id": record.operation_ids[0]}

    result = confirm(service, record, approved_operation_ids=record.operation_ids)

    assert result.outcome is ResumeOutcome.INCONSISTENT and not result.written
    assert storage.batch_attempts == 1  # only the first, failed attempt; no new try
    assert not storage.batches


def test_partial_receipts_log_the_card_and_operations_to_check(caplog):
    service, storage, _, _, _ = setup()
    record = make_batch(service, names=("豆漿", "衛生紙"))
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, record, approved_operation_ids=record.operation_ids)
    storage.receipts[(HOME, record.operation_ids[0])] = {"operation_id": record.operation_ids[0]}

    with caplog.at_level("ERROR", logger="app.services.approvals"):
        result = confirm(service, record, approved_operation_ids=record.operation_ids)

    assert result.outcome is ResumeOutcome.INCONSISTENT
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    line = errors[0].getMessage()
    assert "reconciliation-required" in line
    assert f"household={HOME}" in line and f"approval={record.approval_id}" in line
    assert f"with_receipt=['{record.operation_ids[0]}']" in line
    assert f"without_receipt=['{record.operation_ids[1]}']" in line
    assert storage.batch_attempts == 1  # logging repairs nothing and tries nothing new


def test_a_failed_first_receipt_read_logs_the_card_and_stops(caplog):
    service, storage, _, _, _ = setup()
    record = make_batch(service, names=("豆漿", "衛生紙"))
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    storage.fail_next_receipt_read = RuntimeError("Cosmos unavailable")

    with caplog.at_level("ERROR", logger="app.services.approvals"):
        with pytest.raises(RuntimeError):
            confirm(service, record, approved_operation_ids=record.operation_ids)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    line = errors[0].getMessage()
    assert "reconciliation-required" in line and "could not be read" in line
    assert f"household={HOME}" in line and f"approval={record.approval_id}" in line
    assert f"operations={list(record.operation_ids)}" in line
    assert storage.batch_attempts == 1  # nothing more was tried
    assert not storage.batches
    assert stored_record(storage, record).status is ApprovalStatus.CONFIRMED


def test_timeout_on_the_confirm_step_can_be_retried():
    service, storage, _, _, etag = setup()
    record = make_single(service, etag)
    storage.fail_next_replace = TimeoutError("No response")

    first = confirm(service, record)

    assert first.outcome is ResumeOutcome.NOT_WRITTEN
    assert stored_record(storage, record).status is ApprovalStatus.PENDING

    retry = confirm(service, record)

    assert retry.outcome is ResumeOutcome.COMPLETED


def test_unknown_or_other_household_approval_is_not_found():
    service, _, _, _, etag = setup()
    record = make_single(service, etag)

    with pytest.raises(ApprovalNotFound):
        asyncio.run(
            service.resume(
                ResumeRequest(
                    household_id="someone-else", approval_id=record.approval_id, approved=True
                )
            )
        )
    with pytest.raises(ApprovalNotFound):
        asyncio.run(
            service.resume(ResumeRequest(household_id=HOME, approval_id="unknown", approved=True))
        )


# ----- reminders -----------------------------------------------------------------------


def test_open_cards_for_reminders_and_expired_ones_are_recorded():
    service, storage, _, clock, etag = setup()
    stale_pending = make_batch(service)
    stale_purchase = make_purchase(service)
    retryable = make_single(service, etag)
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, stale_purchase, selected_option_id="option_001")
    storage.fail_next_batch = RuntimeError("Cosmos unavailable")
    confirm(service, retryable)
    finished = make_batch(service, names=("牙膏",))
    confirm(service, finished, approved_operation_ids=finished.operation_ids)
    clock.advance(31)
    fresh = make_batch(service, names=("醬油",))

    open_cards = asyncio.run(service.list_open(household_id=HOME))

    assert {card.approval_id for card in open_cards} == {
        retryable.approval_id,
        fresh.approval_id,
    }
    assert stored_record(storage, stale_pending).status is ApprovalStatus.EXPIRED
    assert stored_record(storage, stale_purchase).status is ApprovalStatus.EXPIRED


# ----- B1 repository fits the port --------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "keywords"),
    [
        ("read_pending_approval", {"household_id", "approval_id"}),
        ("read_operation_receipt", {"household_id", "operation_id"}),
        ("list_open_approvals", {"household_id"}),
        ("execute_idempotent_batches", {"household_id", "operations_by_id", "shared_operations"}),
    ],
)
def test_b1_repository_methods_match_the_port(method, keywords):
    """Verify B1 storage methods match the A1 approval service port."""
    parameters = inspect.signature(getattr(CosmosWriteRepository, method)).parameters

    assert keywords <= set(parameters)
    assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in keywords)
