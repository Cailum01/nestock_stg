"""A1: new-item cards from a photo (Issue #124, contract section 10).

With photo cards on, photo items the household has no item for (no same name or
alias), seen with confidence and with a confirmed unit, get one pending `create_item`
card. Nothing else is written. In the running app photo cards are off until confirming
one can be written, so the Agent there still only reports what the photo shows.
"""

import asyncio
from datetime import UTC, datetime

from langchain_core.messages import AIMessage
from test_purchase_cards import FakeCosmosContainer
from test_read_agent import (
    HOUSEHOLD,
    PHOTO,
    ScriptedModel,
    _ask_photo,
    _call,
    _detected,
    _Inventory,
    _inventory_item,
    _seen_by_model,
    _Shopping,
    _StubVision,
)

from app.agent.cards import PHOTO_CARD_MADE, PhotoCards
from app.agent.read_agent import ReadAgent, load_system_prompt
from app.models.agent import ApprovalPresentation, ResultKind
from app.services.approvals import ApprovalService, DomainWritePlan
from app.services.cosmos_write import CosmosWriteRepository
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.tools.queries import QueryTools

T0 = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)


class _PlanEveryLine:
    """Stands in for the write builder that will write photo items (not built yet)."""

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails

    async def build(self, record):
        if self.fails:
            raise ConnectionError("Cosmos is unreachable")
        return DomainWritePlan(operations_by_id={op: () for op in record.approved_operation_ids})


def _agent(model, inventory, *detected, photo_cards=True, fails=False):
    container = FakeCosmosContainer()
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        _PlanEveryLine(fails),
        clock=lambda: T0,
    )
    media = LocalStagingMediaStorage()
    asyncio.run(media.simulate_client_upload(HOUSEHOLD, PHOTO, b"jpeg-bytes"))
    agent = ReadAgent(
        model,
        QueryTools(inventory, _Shopping()),
        load_system_prompt(),
        vision=_StubVision(*detected),
        media=media,
        photo_cards=PhotoCards(approvals) if photo_cards else None,
    )
    return agent, container


def _photo(*detected, inventory=None, photo_cards=True, fails=False):
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="請確認。"))
    agent, container = _agent(
        model, inventory or _Inventory(), *detected, photo_cards=photo_cards, fails=fails
    )
    response = _ask_photo(agent)
    return response, _seen_by_model(model), container.of_type("pending_approval")


def test_a_new_photo_item_gets_a_pending_create_item_card():
    response, seen, stored = _photo(_detected("滑鼠", unit="個"))

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.SINGLE_ACTION
    [line] = card.operations
    assert line.action == "create_item"
    assert line.description == "會新增一個庫存品項：滑鼠 1 個（照片辨識）"
    assert line.values["name"] == "滑鼠"
    assert line.values["quantity"] == 1
    assert line.values["unit"] == "個"
    assert line.values["location"] is None  # the App shows 未填
    assert line.values["transaction_id"]
    assert "shopping_item_id" not in line.values
    # Only the pending card is stored; the photo result is still shown.
    assert [(doc["approval_id"], doc["status"]) for doc in stored] == [
        (card.approval_id, "pending")
    ]
    assert [result.kind for result in response.results] == [ResultKind.VISION]
    assert seen["new_item_card"] == {
        "lines": ["會新增一個庫存品項：滑鼠 1 個（照片辨識）"],
        "note": PHOTO_CARD_MADE,
    }


def test_several_new_items_share_one_batch_card():
    response, _, stored = _photo(_detected("滑鼠", unit="個"), _detected("鍵盤", unit="個"))

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.BATCH_ACTION
    assert [line.values["name"] for line in card.operations] == ["滑鼠", "鍵盤"]
    assert len(stored) == 1


def test_items_the_household_has_get_no_line():
    soy = _inventory_item("soy", "醬油", "龜甲萬")
    response, seen, _ = _photo(
        _detected("醬油"),
        _detected("龜甲萬"),
        _detected("滑鼠", unit="個"),
        inventory=_Inventory(soy),
    )

    assert [line.values["name"] for line in response.pending_approval.operations] == ["滑鼠"]


def test_only_known_items_make_no_card():
    soy = _inventory_item("soy", "醬油", "龜甲萬")
    response, seen, stored = _photo(_detected("醬油"), inventory=_Inventory(soy))

    assert response.pending_approval is None
    assert stored == []
    assert "new_item_card" not in seen


def test_unsure_items_make_no_card():
    response, _, stored = _photo(
        _detected("滑鼠", unit="個", confidence=0.4),
        _detected("鍵盤", unit="個", unit_confirmed=False),
        _detected("螢幕", unit=None),
    )

    assert response.pending_approval is None
    assert stored == []


def test_with_photo_cards_off_the_photo_is_only_reported():
    """How the running app behaves until photo items can be written (Issue #124)."""
    response, seen, stored = _photo(_detected("滑鼠", unit="個"), photo_cards=False)

    assert response.pending_approval is None
    assert stored == []
    assert "new_item_card" not in seen
    assert [result.kind for result in response.results] == [ResultKind.VISION]


def test_a_card_that_cannot_be_stored_is_not_shown():
    response, seen, stored = _photo(_detected("滑鼠", unit="個"), fails=True)

    assert response.pending_approval is None
    assert stored == []
    assert "new_item_card" not in seen
    assert [result.kind for result in response.results] == [ResultKind.VISION]


def test_one_card_per_message():
    model = ScriptedModel(
        _call("analyze_photo", "call-1"),
        _call("analyze_photo", "call-2"),
        AIMessage(content="請確認。"),
    )
    agent, container = _agent(model, _Inventory(), _detected("滑鼠", unit="個"))

    response = _ask_photo(agent)

    assert response.pending_approval is not None
    assert len(container.of_type("pending_approval")) == 1
