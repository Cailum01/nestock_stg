"""A1: a photo new item from the photo to the written record (Issue #124, #168).

Through the app's own composition (`_create_approval_service`, PR #171): the Agent
makes the photo card on `/agent/messages`, and `/agent/resume` writes the confirmed
lines through A1's `CardWriteRouter` to B1's no-Shopping writer. Same faked Cosmos
container as `test_purchase_cards`; the model and Vision are stubs.
"""

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_purchase_cards import HOME, FakeCosmosContainer, inventory, resume_app, setup
from test_read_agent import PHOTO, ScriptedModel, _ask_photo, _call, _detected, _StubVision

from app.agent import read_agent
from app.agent.cards import PhotoCards
from app.agent.read_agent import ReadAgent, load_system_prompt
from app.core.dependencies import _create_approval_service
from app.services.cosmos_read import (
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
)
from app.services.cosmos_write import CosmosWriteRepository
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.tools.queries import QueryTools

T0 = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)


def _app_approvals(container: FakeCosmosContainer):
    reads = CosmosReadRepository(container)
    writes = CosmosWriteRepository(container, query_client=container)
    return reads, _create_approval_service(reads, writes, container)


def _photo_agent(container, reads, approvals, *detected):
    media = LocalStagingMediaStorage()
    asyncio.run(media.simulate_client_upload(HOME, PHOTO, b"jpeg-bytes"))
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="請確認。"))
    return ReadAgent(
        model,
        QueryTools(CosmosInventoryService(reads), CosmosShoppingService(reads)),
        load_system_prompt(),
        vision=_StubVision(*detected),
        media=media,
        photo_cards=PhotoCards(approvals),
    )


def _confirm(approvals, card, ids=None):
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": True}
    if ids is not None:
        body["approved_operation_ids"] = ids
    with TestClient(resume_app(approvals)) as client:
        return client.post("/agent/resume", json=body).json()


def _new_items(container, *names):
    return [d for d in container.of_type("inventory_item") if d["name"] in names]


def test_a_photo_item_is_written_after_the_user_confirms():
    container, _, _ = setup()
    reads, approvals = _app_approvals(container)
    agent = _photo_agent(container, reads, approvals, _detected("滑鼠", unit="個", quantity=2))
    shopping_before = container.of_type("shopping_item")

    card = _ask_photo(agent, household_id=HOME).pending_approval
    assert card is not None and _new_items(container, "滑鼠") == []  # nothing before confirming

    reply = _confirm(approvals, card)

    assert reply["message"] == "已記錄：滑鼠 +2 個（現在 2 個）"
    (item,) = _new_items(container, "滑鼠")
    assert (item["quantity"], item["unit"], item["location"]) == (2, "個", None)
    (tx,) = [d for d in container.of_type("transaction") if d["item_id"] == item["item_id"]]
    assert (tx["reason"], tx["quantity_delta"]) == ("adjustment", 2)
    (event,) = [d for d in container.of_type("change_event") if d["item_id"] == item["item_id"]]
    assert event["shopping_item_id"] is None
    assert container.of_type("shopping_item") == shopping_before  # no Shopping row

    again = _confirm(approvals, card)  # the same answer again: nothing more is written
    assert again["message"] == reply["message"]
    assert len(_new_items(container, "滑鼠")) == 1


def test_only_the_ticked_photo_lines_are_written():
    container, _, _ = setup()
    reads, approvals = _app_approvals(container)
    agent = _photo_agent(
        container,
        reads,
        approvals,
        _detected("滑鼠", unit="個"),
        _detected("鍵盤", unit="個"),
    )
    card = _ask_photo(agent, household_id=HOME).pending_approval
    mouse, _ = card.operations

    _confirm(approvals, card, ids=[mouse.operation_id])

    assert [d["name"] for d in _new_items(container, "滑鼠", "鍵盤")] == ["滑鼠"]


def test_a_photo_item_added_after_the_card_is_not_added_twice():
    """PR #187 B1 review: confirming reads the household's items again, as for add_stock."""
    container, _, _ = setup()
    reads, approvals = _app_approvals(container)
    agent = _photo_agent(container, reads, approvals, _detected("滑鼠", unit="個"))
    card = _ask_photo(agent, household_id=HOME).pending_approval
    container.put(inventory(item_id="mouse", name="無線滑鼠", unit="個", aliases=["滑鼠"]))
    transactions_before = container.of_type("transaction")

    reply = _confirm(approvals, card)

    assert reply["results"][0]["data"] == {"outcome": "needs_new_card"}
    assert [d["item_id"] for d in container.of_type("inventory_item") if d["unit"] == "個"] == [
        "mouse"
    ]
    assert container.of_type("transaction") == transactions_before


def test_a_line_the_household_has_by_now_does_not_stop_the_other_ticked_lines():
    """Only the ticked lines are checked: an unticked one the household has is left out."""
    container, _, _ = setup()
    reads, approvals = _app_approvals(container)
    agent = _photo_agent(
        container,
        reads,
        approvals,
        _detected("滑鼠", unit="個"),
        _detected("鍵盤", unit="個"),
    )
    card = _ask_photo(agent, household_id=HOME).pending_approval
    mouse, _ = card.operations
    container.put(inventory(item_id="keyboard", name="鍵盤", unit="個"))

    reply = _confirm(approvals, card, ids=[mouse.operation_id])

    assert reply["message"] == "已記錄：滑鼠 +1 個（現在 1 個）"
    assert sorted(d["name"] for d in _new_items(container, "滑鼠", "鍵盤")) == ["滑鼠", "鍵盤"]


def test_get_read_agent_gives_photo_cards_the_app_approval_service(monkeypatch):
    container = FakeCosmosContainer()
    _, approvals = _app_approvals(container)
    monkeypatch.setattr(read_agent, "get_approval_service", lambda: approvals)

    cards = read_agent._photo_cards()

    assert isinstance(cards, PhotoCards)
    assert cards._approvals is approvals


def test_without_cosmos_there_are_no_photo_cards(monkeypatch):
    monkeypatch.setattr(read_agent, "get_approval_service", lambda: None)

    assert read_agent._photo_cards() is None
