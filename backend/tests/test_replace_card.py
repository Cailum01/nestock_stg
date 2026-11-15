"""A1: 送出修改 on a card (contract section 7, Issue #91).

The App sends the change to `POST /agent/messages` with `replaces_approval_id`. The
Agent gets the old card, makes a new one, and the old card is cancelled only after the
new one is stored. Same faked Cosmos container and real services as
`test_purchase_cards`.
"""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage
from test_purchase_cards import (
    HOME,
    T0,
    ScriptedModel,
    agent,
    purchase_reply,
    setup,
)

from app.agent.read_agent import (
    CARD_CHANGE_NOT_DONE,
    CARD_CHANGE_REFUSED,
    CARD_CHANGE_UNAVAILABLE,
    ReadAgent,
    get_read_agent,
    load_system_prompt,
)
from app.main import create_app
from app.models.agent import AgentRequest, ResumeRequest
from app.services.approvals import ApprovalRecord, ApprovalService, ResumeOutcome
from app.services.cosmos_read import CosmosInventoryService, CosmosShoppingService
from app.services.cosmos_write import CosmosWriteRepository
from app.services.purchase_inputs import CardInputResolver
from app.services.shopping.workflow import ShoppingWriteBuilder
from app.tools.queries import QueryTools


def run(coro):
    return asyncio.run(coro)


def first_card(reader, approvals, quantity=2):
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": quantity}, call_id="call-1"),
        AIMessage(content="請確認"),
    )
    request = AgentRequest(household_id=HOME, message=f"我買了衛生紙 {quantity} 包")
    return run(agent(model, reader, approvals).respond(request)).pending_approval


def change(the_agent, approval_id, message="衛生紙：數量改成 1", household_id=HOME):
    request = AgentRequest(
        household_id=household_id, message=message, replaces_approval_id=approval_id
    )
    return run(the_agent.respond(request))


def status(container, approval_id):
    document = container.get(f"pending_approval:{approval_id}")
    return document["status"], document.get("cancel_reason")


def test_send_change_makes_a_new_card_and_cancels_the_old_one():
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="已改成 1 包，請確認新的卡片。"),
    )

    response = change(agent(model, reader, approvals), old.approval_id)

    new = response.pending_approval
    assert new.approval_id != old.approval_id
    assert new.operations[0].description == "衛生紙 +1 包（從待購清單完成購買）"
    assert new.operations[0].operation_id != old.operations[0].operation_id
    assert status(container, new.approval_id) == ("pending", None)
    assert status(container, old.approval_id) == ("cancelled", "replaced")
    assert container.get("inventory_item:tissue")["quantity"] == 1  # nothing written
    shown = [m.content for m in model.seen[0] if isinstance(m, SystemMessage)][-1]
    assert "送出修改" in shown and "tissue-1" in shown
    assert old.operations[0].description in shown
    assert "transaction_id" not in shown


def test_the_route_takes_replaces_approval_id():
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="請確認新的卡片。"),
    )
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: agent(model, reader, approvals)
    body = {
        "household_id": HOME,
        "message": "衛生紙：數量改成 1",
        "replaces_approval_id": old.approval_id,
    }

    with TestClient(app) as client:
        response = client.post("/agent/messages", json=body)

    assert response.status_code == 200
    assert response.json()["pending_approval"]["approval_id"] != old.approval_id
    assert status(container, old.approval_id) == ("cancelled", "replaced")


def test_no_new_card_leaves_the_old_card_open():
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    model = ScriptedModel(AIMessage(content="要改成幾包？"))

    response = change(agent(model, reader, approvals), old.approval_id, "衛生紙：數量改成")

    assert response.pending_approval is None
    assert status(container, old.approval_id) == ("pending", None)


def test_a_new_card_that_cannot_be_stored_leaves_the_old_card_open():
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 5}, call_id="call-1"),
        AIMessage(content="買的數量比待購清單多，現在還不能記錄。"),
    )

    response = change(agent(model, reader, approvals), old.approval_id, "衛生紙：數量改成 5")

    assert response.pending_approval is None
    assert status(container, old.approval_id) == ("pending", None)


def _cards(container):
    return {doc["approval_id"]: doc for doc in container.of_type("pending_approval")}


def _change_with_old_card_stuck(monkeypatch, container, reader, approvals, old):
    """送出修改 where cancelling the old card fails (Cosmos error injected)."""

    async def fails(**_):
        raise ConnectionError("Cosmos is unreachable")

    monkeypatch.setattr(approvals, "cancel_replaced", fails)
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="請確認新的卡片。"),
    )
    return change(agent(model, reader, approvals), old.approval_id)


def test_if_the_old_card_cannot_be_cancelled_the_new_card_is_withdrawn(monkeypatch):
    container, reader, approvals = setup()
    old = first_card(reader, approvals)

    response = _change_with_old_card_stuck(monkeypatch, container, reader, approvals, old)

    assert response.pending_approval is None
    assert response.message == CARD_CHANGE_NOT_DONE
    cards = _cards(container)
    [new_id] = [approval_id for approval_id in cards if approval_id != old.approval_id]
    assert status(container, new_id) == ("cancelled", "replace_failed")
    # Only the old card can still be answered, and it still writes as shown.
    assert status(container, old.approval_id) == ("pending", None)
    _confirmed(approvals, old)
    assert container.get("inventory_item:tissue")["quantity"] == 3


def test_a_cancel_written_before_its_reply_was_lost_still_shows_the_new_card(monkeypatch):
    """Commit then timeout (PR #143 B1 review): the old card is read back as cancelled."""
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    real_cancel = approvals.cancel_replaced

    async def written_then_timeout(**kwargs):
        await real_cancel(**kwargs)
        raise TimeoutError("reply lost after the write")

    monkeypatch.setattr(approvals, "cancel_replaced", written_then_timeout)
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="請確認新的卡片。"),
    )

    response = change(agent(model, reader, approvals), old.approval_id)

    assert response.pending_approval is not None
    assert status(container, old.approval_id) == ("cancelled", "replaced")
    assert status(container, response.pending_approval.approval_id) == ("pending", None)


def test_if_the_new_card_cannot_be_withdrawn_either_it_is_not_shown(monkeypatch, caplog):
    container, reader, approvals = setup()
    old = first_card(reader, approvals)

    async def also_fails(**_):
        raise ConnectionError("Cosmos is unreachable")

    monkeypatch.setattr(approvals, "cancel_failed_replacement", also_fails)

    response = _change_with_old_card_stuck(monkeypatch, container, reader, approvals, old)

    assert response.pending_approval is None
    assert response.message == CARD_CHANGE_NOT_DONE
    assert "is not shown and expires on its own" in caplog.text
    assert status(container, old.approval_id) == ("pending", None)


def _confirmed(approvals, card):
    run(
        approvals.resume(
            ResumeRequest(household_id=HOME, approval_id=card.approval_id, approved=True)
        )
    )


def _cancelled(approvals, card):
    run(
        approvals.resume(
            ResumeRequest(household_id=HOME, approval_id=card.approval_id, approved=False)
        )
    )


@pytest.mark.parametrize(
    "before, reason",
    [(_confirmed, "completed"), (_cancelled, "cancelled")],
)
def test_an_answered_card_gets_no_new_card_and_the_model_is_not_asked(before, reason):
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    before(approvals, old)
    model = ScriptedModel()  # any model call would fail: no replies prepared

    response = change(agent(model, reader, approvals), old.approval_id)

    assert response.message == CARD_CHANGE_REFUSED[reason]
    assert response.pending_approval is None
    assert model.seen == []


@pytest.mark.parametrize(
    "approval_id, household_id", [("no-such-card", HOME), (None, "another-household")]
)
def test_an_unknown_or_other_household_card_is_not_found(approval_id, household_id):
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    model = ScriptedModel()

    response = change(
        agent(model, reader, approvals), approval_id or old.approval_id, household_id=household_id
    )

    assert response.message == CARD_CHANGE_REFUSED["not_found"]
    assert status(container, old.approval_id) == ("pending", None)


def test_an_expired_card_gets_no_new_card_and_is_recorded_as_expired():
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    later = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        ShoppingWriteBuilder(container, CardInputResolver(reader)),
        clock=lambda: T0 + timedelta(hours=1),
    )
    model = ScriptedModel()

    response = change(agent(model, reader, later), old.approval_id)

    assert response.message == CARD_CHANGE_REFUSED["expired"]
    assert status(container, old.approval_id)[0] == "expired"


def test_without_cards_a_change_is_refused():
    _, reader, _ = setup()
    tools = QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader))
    model = ScriptedModel()

    response = change(ReadAgent(model, tools, load_system_prompt()), "any-card")

    assert response.message == CARD_CHANGE_UNAVAILABLE


# ----- 「取代算數」: a 送出修改 card counts only once its original is replaced -----------
# PR #143 C2 review: even when cancelling the old card and withdrawing the new card both
# fail, the new card must never be listed (section 8 reminders, typed changes) or confirmed.


def _resume(approvals, approval_id, approved=True):
    return run(
        approvals.resume(
            ResumeRequest(household_id=HOME, approval_id=approval_id, approved=approved)
        )
    )


def _listed(approvals):
    return [card.approval_id for card in run(approvals.list_open(household_id=HOME))]


def test_after_both_cancels_fail_the_new_card_is_never_listed_or_confirmed(monkeypatch):
    container, reader, approvals = setup()
    old = first_card(reader, approvals)

    async def also_fails(**_):
        raise ConnectionError("Cosmos is unreachable")

    monkeypatch.setattr(approvals, "cancel_failed_replacement", also_fails)
    response = _change_with_old_card_stuck(monkeypatch, container, reader, approvals, old)

    assert response.pending_approval is None
    [new_id] = [approval_id for approval_id in _cards(container) if approval_id != old.approval_id]
    assert status(container, new_id) == ("pending", None)  # left behind, as in the review
    assert _cards(container)[new_id]["replaces_approval_id"] == old.approval_id

    # The next message: the lookup reminders and typed changes use lists only the old card.
    model = ScriptedModel(AIMessage(content="好的"))
    later = run(
        agent(model, reader, approvals).respond(AgentRequest(household_id=HOME, message="嗨"))
    )
    assert later.pending_approval is None
    assert _listed(approvals) == [old.approval_id]

    # Confirming the hidden card writes nothing; it is closed instead.
    result = _resume(approvals, new_id)
    assert result.outcome is ResumeOutcome.CANCELLED
    assert status(container, new_id) == ("cancelled", "replace_failed")
    assert container.get("inventory_item:tissue")["quantity"] == 1

    # The original still works, once.
    assert _resume(approvals, old.approval_id).written
    assert container.get("inventory_item:tissue")["quantity"] == 3
    assert _resume(approvals, new_id).outcome is ResumeOutcome.CANCELLED
    assert container.get("inventory_item:tissue")["quantity"] == 3


def test_a_replacement_counts_once_the_old_card_is_replaced():
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    assert _cards(container)[old.approval_id]["replaces_approval_id"] is None
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="請確認新的卡片。"),
    )

    new = change(agent(model, reader, approvals), old.approval_id).pending_approval

    assert _cards(container)[new.approval_id]["replaces_approval_id"] == old.approval_id
    assert _listed(approvals) == [new.approval_id]
    assert _resume(approvals, new.approval_id).written
    assert container.get("inventory_item:tissue")["quantity"] == 2


def test_if_the_old_card_expired_meanwhile_the_new_card_is_withdrawn(monkeypatch):
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    real_cancel = approvals.cancel_replaced

    async def expired_first(**kwargs):
        # Someone else recorded the old card as expired while the Agent was working.
        stored = await approvals._load(HOME, old.approval_id)
        await approvals._record_expired(ApprovalRecord.from_document(stored.document), stored.etag)
        return await real_cancel(**kwargs)

    monkeypatch.setattr(approvals, "cancel_replaced", expired_first)
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="請確認新的卡片。"),
    )

    response = change(agent(model, reader, approvals), old.approval_id)

    assert response.pending_approval is None
    assert response.message == CARD_CHANGE_REFUSED["expired"]
    [new_id] = [approval_id for approval_id in _cards(container) if approval_id != old.approval_id]
    assert status(container, new_id) == ("cancelled", "replace_failed")
    assert _listed(approvals) == []


def test_on_read_back_only_a_replaced_old_card_counts(monkeypatch):
    """The cancel raised; the old card reads back cancelled, but by the user: not replaced."""
    container, reader, approvals = setup()
    old = first_card(reader, approvals)

    async def user_cancelled_then_error(**_):
        await approvals.resume(
            ResumeRequest(household_id=HOME, approval_id=old.approval_id, approved=False)
        )
        raise TimeoutError("reply lost")

    monkeypatch.setattr(approvals, "cancel_replaced", user_cancelled_then_error)
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="請確認新的卡片。"),
    )

    response = change(agent(model, reader, approvals), old.approval_id)

    assert response.pending_approval is None
    assert response.message == CARD_CHANGE_REFUSED["cancelled"]
    assert status(container, old.approval_id) == ("cancelled", "user")
    [new_id] = [approval_id for approval_id in _cards(container) if approval_id != old.approval_id]
    assert status(container, new_id) == ("cancelled", "replace_failed")


def test_a_card_stored_before_this_field_still_reads_and_counts():
    container, reader, approvals = setup()
    old = first_card(reader, approvals)
    document = dict(_cards(container)[old.approval_id])
    del document["replaces_approval_id"]

    record = ApprovalRecord.from_document(document)

    assert record.replaces_approval_id is None
    assert run(approvals.counts(record))
