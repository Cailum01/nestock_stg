"""A1: confirming a photo new-item card (Issue #124, contract section 10).

`PhotoInputResolver` turns each confirmed line into C1's `AdjustmentInput` from the
stored card alone, and `CardWriteRouter` hands photo cards to the builder that will
write them (B1's, not built yet) while purchase cards keep going to C1's builder.
Same faked Cosmos container and real approval service as `test_purchase_cards`.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_purchase_cards import HOME, FakeCosmosContainer, made_card, resume_app, setup

from app.agent.cards import PhotoCards, PhotoItem
from app.models.agent import ResumeRequest
from app.services.approvals import ApprovalRecord, ApprovalService, DomainWritePlan, TargetVersion
from app.services.card_writes import PHOTO_WRITE_MISSING, CardWriteRouter
from app.services.cosmos_write import CosmosWriteRepository
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.purchase_inputs import (
    CardInputResolver,
    PhotoInputResolver,
    UnsupportedAction,
    is_photo_card,
)
from app.services.shopping.planning import ShoppingPlanService
from app.services.shopping.workflow import ShoppingWriteBuilder

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class _Recording:
    """A builder that records the cards it was given and writes nothing."""

    def __init__(self) -> None:
        self.cards: list[str] = []

    async def build(self, record: ApprovalRecord) -> DomainWritePlan:
        self.cards.append(record.approval_id)
        ids = record.approved_operation_ids or record.operation_ids  # `save` plans a trial
        return DomainWritePlan(operations_by_id={op: () for op in ids})


def _photo_card(*names, approved=None):
    """A stored photo card for `names` (quantity 2 each), confirmed for `approved` lines."""
    container = FakeCosmosContainer()
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container), _Recording(), clock=lambda: T0
    )
    card = asyncio.run(
        PhotoCards(approvals).propose(HOME, [PhotoItem(name, 2, "個") for name in names])
    )
    record = ApprovalRecord.from_document(container.get(f"pending_approval:{card['approval_id']}"))
    ids = record.operation_ids if approved is None else approved(record.operation_ids)
    return record.model_copy(update={"approved_operation_ids": ids, "decided_at": T0})


def test_each_confirmed_photo_line_becomes_c1_adjustment_input():
    record = _photo_card("滑鼠")
    (line,) = record.operations

    (adjustment,) = PhotoInputResolver().resolve(record)

    assert adjustment.approval_id == record.approval_id
    assert adjustment.operation_id == line.operation_id
    assert adjustment.transaction_id == line.values["transaction_id"]
    assert adjustment.quantity == 2
    target = adjustment.new_inventory
    assert target.item_id == resolve_inventory_item_id(line.operation_id)  # retry: same item
    assert (target.name, target.unit, target.location) == ("滑鼠", "個", None)


def test_only_the_ticked_lines_in_card_order():
    record = _photo_card("滑鼠", "鍵盤", "螢幕", approved=lambda ids: [ids[2], ids[0]])

    resolved = PhotoInputResolver().resolve(record)

    assert [a.new_inventory.name for a in resolved] == ["滑鼠", "螢幕"]


def test_c1_plans_the_resolved_line():
    """C1's `plan_adjustment` accepts it, B1's stable item_id check included."""
    record = _photo_card("滑鼠")
    (adjustment,) = PhotoInputResolver().resolve(record)

    plan = ShoppingPlanService().plan_adjustment(
        household_id=HOME, adjustment=adjustment, recorded_at=record.decided_at
    )

    assert plan.inventory_create.record.name == "滑鼠"
    assert plan.inventory_create.record.quantity == 2
    assert plan.transaction.reason.value == "adjustment"
    assert plan.transaction.transaction_id == adjustment.transaction_id
    assert plan.transaction.created_at == T0  # the card's decided_at


@pytest.mark.parametrize(
    ("field", "value"),
    [("quantity", 0), ("quantity", "2"), ("name", ""), ("unit", None), ("transaction_id", None)],
)
def test_a_photo_line_with_unusable_values_is_refused(field, value):
    record = _photo_card("滑鼠")
    (line,) = record.operations
    broken = line.model_copy(update={"values": {**line.values, field: value}})

    with pytest.raises(ValueError):
        PhotoInputResolver().resolve(record.model_copy(update={"operations": [broken]}))


def test_a_photo_line_with_record_versions_is_refused():
    """A photo line adds a new item: there is no existing record whose version it holds."""
    record = _photo_card("滑鼠")
    (line,) = record.operations
    versioned = line.model_copy(
        update={"targets": [TargetVersion(record_id="inventory_item:mouse", etag='"e1"')]}
    )

    with pytest.raises(ValueError, match="no versions"):
        PhotoInputResolver().resolve(record.model_copy(update={"operations": [versioned]}))


def test_a_purchase_card_is_not_a_photo_card():
    _, reader, approvals = setup()
    card = made_card(reader, approvals)
    record = asyncio.run(approvals.read_card(household_id=HOME, approval_id=card.approval_id))

    assert not is_photo_card(record)
    assert is_photo_card(_photo_card("滑鼠"))
    with pytest.raises(UnsupportedAction):
        PhotoInputResolver().resolve(record)


# ----- CardWriteRouter: which builder plans a confirmed card ----------------------


def test_the_router_sends_photo_cards_to_the_photo_builder():
    purchases, photos = _Recording(), _Recording()
    record = _photo_card("滑鼠")

    asyncio.run(CardWriteRouter(purchases, photos).build(record))

    assert (purchases.cards, photos.cards) == ([], [record.approval_id])


def test_the_router_keeps_purchase_cards_on_the_purchase_builder():
    _, reader, approvals = setup()
    card = made_card(reader, approvals)
    record = asyncio.run(approvals.read_card(household_id=HOME, approval_id=card.approval_id))
    purchases, photos = _Recording(), _Recording()

    asyncio.run(CardWriteRouter(purchases, photos).build(record))

    assert (purchases.cards, photos.cards) == ([card.approval_id], [])


def test_without_the_photo_builder_a_photo_card_writes_nothing():
    """Until B1's builder exists: refused before anything is stored or written."""
    container, reader, _ = setup()
    repository = CosmosWriteRepository(container, query_client=container)
    stored_by = ApprovalService(repository, _Recording(), clock=lambda: T0)
    card = asyncio.run(PhotoCards(stored_by).propose(HOME, [PhotoItem("滑鼠", 2, "個")]))
    approvals = ApprovalService(
        repository,
        CardWriteRouter(ShoppingWriteBuilder(container, CardInputResolver(reader))),
        clock=lambda: T0,
    )
    before = {key: dict(doc) for key, doc in container.documents.items()}

    with pytest.raises(UnsupportedAction, match="Issue #124"):
        asyncio.run(
            approvals.resume(
                ResumeRequest(household_id=HOME, approval_id=card["approval_id"], approved=True)
            )
        )

    assert container.documents == before  # the card stays pending; nothing written
    assert PHOTO_WRITE_MISSING.endswith("(Issue #124).")


def test_without_the_photo_builder_a_photo_card_is_not_even_stored():
    """`save` plans a trial write, so the router refuses a photo card before it is shown."""
    container, reader, _ = setup()
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        CardWriteRouter(ShoppingWriteBuilder(container, CardInputResolver(reader))),
        clock=lambda: T0,
    )

    card = asyncio.run(PhotoCards(approvals).propose(HOME, [PhotoItem("滑鼠", 2, "個")]))

    assert card is None
    assert container.of_type("pending_approval") == []


def test_purchase_cards_still_confirm_through_the_router():
    """The router changes nothing for 買回來了 (v8 flow C)."""
    container, reader, _ = setup()
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        CardWriteRouter(ShoppingWriteBuilder(container, CardInputResolver(reader))),
        clock=lambda: T0,
    )
    card = made_card(reader, approvals)

    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": True}
    with TestClient(resume_app(approvals)) as client:
        reply = client.post("/agent/resume", json=body).json()

    assert reply["message"] == "已記錄：衛生紙 +2 包（現在 3 包）"
