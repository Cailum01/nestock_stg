"""A1: new-item cards from a photo (Issue #124, contract section 10).

With photo cards on, photo items the household has no item for (no same name or
alias), seen with confidence and with a confirmed unit, get one pending `create_item`
card. Nothing else is written. Confirming one is in `test_photo_end_to_end.py`.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage
from test_purchase_cards import FakeCosmosContainer, setup
from test_purchase_cards import inventory as _purchase_inventory
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

from app.agent.cards import (
    CARD_CHANGED,
    CHANGE_NOT_THIS_CARD,
    CHANGE_NOT_THIS_LINE,
    PHOTO_CARD_MADE,
    PHOTO_NOT_READY,
    PhotoCards,
    PhotoItem,
    PurchaseCards,
)
from app.agent.read_agent import OPEN_CARD_PENDING, ReadAgent, load_system_prompt
from app.models.agent import AgentRequest, ApprovalPresentation, ResultKind
from app.services.approvals import ApprovalService, DomainWritePlan
from app.services.cosmos_read import CosmosInventoryService, CosmosShoppingService
from app.services.cosmos_write import CosmosWriteRepository
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.tools.queries import QueryTools

T0 = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)


class _PlanEveryLine:
    """Stands in for B1's photo item writer: plans every line, writes nothing."""

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
    # This local storage has no mapping to read, so the card keeps no photo (Issue #176).
    assert not {"image_ref", "input_id", "mapping_etag"} & line.values.keys()
    assert seen["new_item_card"] == {
        "lines": ["會新增一個庫存品項：滑鼠 1 個（照片辨識）"],
        "note": PHOTO_CARD_MADE,
        "photo": PHOTO_NOT_READY,
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


# ----- with purchase cards too: open cards (contract section 8, PR #160) -----------


def _photo_with_purchases():
    """A household with a tissue entry on the list, photo cards on and purchase cards."""
    container, reader, _ = setup()
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        _PlanEveryLine(),
        clock=lambda: T0,
    )
    media = LocalStagingMediaStorage()
    asyncio.run(media.simulate_client_upload(HOUSEHOLD, PHOTO, b"jpeg-bytes"))
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="看到滑鼠。"))
    purchases = PurchaseCards(reader, approvals)
    agent = ReadAgent(
        model,
        QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader)),
        load_system_prompt(),
        vision=_StubVision(_detected("滑鼠", unit="個")),
        media=media,
        purchases=purchases,
        photo_cards=PhotoCards(approvals),
    )
    return container, approvals, purchases, agent, model


def _stored(container):
    return [(d["approval_id"], d["status"]) for d in container.of_type("pending_approval")]


def test_no_photo_card_while_an_open_card_is_shown():
    """The message that brings an open card back makes no new card, a photo included."""
    container, _, purchases, agent, model = _photo_with_purchases()
    _, open_card = asyncio.run(
        purchases.propose(HOUSEHOLD, {"items": [{"shopping_item_id": "tissue-1", "quantity": 2}]})
    )

    response = _ask_photo(agent)

    assert response.pending_approval.model_dump(mode="json") == open_card
    assert _stored(container) == [(open_card["approval_id"], "pending")]
    assert "new_item_card" not in _seen_by_model(model)
    assert [result.kind for result in response.results] == [
        ResultKind.VISION,
        ResultKind.ACTION_RESULT,
    ]
    assert response.results[-1].data == {"code": OPEN_CARD_PENDING, "open_cards": 1}


def test_no_photo_card_when_the_open_cards_cannot_be_listed(monkeypatch):
    """Unknown is not "none": a card may still be open (PR #160 C2 review)."""
    container, approvals, _, agent, model = _photo_with_purchases()

    async def unreachable(**_):
        raise ConnectionError("Cosmos is unreachable")

    monkeypatch.setattr(approvals, "list_open", unreachable)
    response = _ask_photo(agent)

    assert response.pending_approval is None
    assert _stored(container) == []
    assert "new_item_card" not in _seen_by_model(model)
    assert [result.kind for result in response.results] == [ResultKind.VISION]


# ----- 送出修改 on a photo card (Issue #124 C2 question, contract section 7.1) -------


def _photo_card_to_change(*names, photo_cards=True):
    """A stored photo card for `names` (1 個 each); a household with only tissue."""
    container, reader, _ = setup()
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        _PlanEveryLine(),
        clock=lambda: T0,
    )
    cards = PhotoCards(approvals)
    old = asyncio.run(cards.propose(HOUSEHOLD, [PhotoItem(name, 1, "個") for name in names]))
    model = ScriptedModel()  # any model call would fail: there are no replies
    agent = ReadAgent(
        model,
        QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader)),
        load_system_prompt(),
        purchases=PurchaseCards(reader, approvals),
        photo_cards=cards if photo_cards else None,
    )
    return container, agent, model, old


def _send_change(agent, model, old, *changes):
    request = AgentRequest(
        household_id=HOUSEHOLD,
        message="送出修改",
        replaces_approval_id=old["approval_id"],
        card_changes=list(changes),
    )
    response = asyncio.run(agent.respond(request))
    assert model.seen == []
    return response


def _ops(card):
    return card["operations"]


def test_a_photo_card_quantity_change_makes_a_new_card_without_the_model():
    container, agent, model, old = _photo_card_to_change("滑鼠")
    (line,) = _ops(old)

    response = _send_change(
        agent, model, old, {"operation_id": line["operation_id"], "quantity": 3}
    )

    assert response.message == CARD_CHANGED
    assert response.results[0].data == {
        "code": "card_changed",
        "replaced_approval_id": old["approval_id"],
    }
    new = response.pending_approval
    assert new.presentation is ApprovalPresentation.SINGLE_ACTION
    (new_line,) = new.operations
    assert new_line.action == "create_item"
    assert new_line.description == "會新增一個庫存品項：滑鼠 3 個（照片辨識）"
    assert {k: v for k, v in new_line.values.items() if k != "transaction_id"} == {
        "name": "滑鼠",
        "quantity": 3,
        "unit": "個",
        "location": None,
    }
    # New identity for the new card; the old card is retired as replaced (section 7).
    assert new.approval_id != old["approval_id"]
    assert new_line.operation_id != line["operation_id"]
    assert new_line.values["transaction_id"] != line["values"]["transaction_id"]
    assert _stored(container) == [
        (old["approval_id"], "cancelled"),
        (new.approval_id, "pending"),
    ]
    stored = container.get(f"pending_approval:{new.approval_id}", HOUSEHOLD)
    assert stored["replaces_approval_id"] == old["approval_id"]


def test_photo_lines_left_out_of_the_change_keep_their_values():
    _, agent, model, old = _photo_card_to_change("滑鼠", "鍵盤")
    mouse, keyboard = _ops(old)

    new = _send_change(
        agent, model, old, {"operation_id": keyboard["operation_id"], "quantity": 2}
    ).pending_approval

    assert new.presentation is ApprovalPresentation.BATCH_ACTION
    assert [(op.values["name"], op.values["quantity"]) for op in new.operations] == [
        ("滑鼠", 1),
        ("鍵盤", 2),
    ]
    assert all(op.values["location"] is None for op in new.operations)
    assert all("shopping_item_id" not in op.values for op in new.operations)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ([{"operation_id": "nope", "quantity": 2}], "unknown_operation"),
        ([{"operation_id": "LINE", "quantity": 2}] * 2, "duplicate_operation"),
        ([{"operation_id": "LINE", "quantity": 1}], "nothing_changed"),
        ([{"operation_id": "LINE", "purchased_on": "2026-11-14"}], "line_refused"),
        ([{"operation_id": "LINE", "quantity": 2, "purchased_on": None}], "line_refused"),
        ([{"operation_id": "LINE", "name": " 滑鼠 "}], "nothing_changed"),
        ([{"operation_id": "LINE", "location": None}], "nothing_changed"),
    ],
)
def test_a_refused_photo_change_stores_nothing_and_shows_the_old_card_again(changes, expected):
    container, agent, model, old = _photo_card_to_change("滑鼠")
    (line,) = _ops(old)
    said = [
        {**c, "operation_id": line["operation_id"]} if c["operation_id"] == "LINE" else c
        for c in changes
    ]

    response = _send_change(agent, model, old, *said)

    assert response.results[0].data["code"] == expected
    assert response.pending_approval.model_dump(mode="json") == old
    assert _stored(container) == [(old["approval_id"], "pending")]


def test_a_photo_line_has_no_day_to_change():
    _, agent, model, old = _photo_card_to_change("滑鼠")
    (line,) = _ops(old)

    response = _send_change(
        agent, model, old, {"operation_id": line["operation_id"], "purchased_on": "2026-11-14"}
    )

    assert response.message == "沒有出新卡：「滑鼠」照片新增的品項沒有購買日期，不能改日期。"
    assert response.results[0].data["lines"] == [
        {"operation_id": line["operation_id"], "reason": "照片新增的品項沒有購買日期，不能改日期。"}
    ]


@pytest.mark.parametrize("aliases", [None, ["滑鼠"]])
def test_a_photo_line_the_household_has_by_now_is_refused(aliases):
    """Someone added the item after the card was made: confirming would add a second."""
    name = "滑鼠" if aliases is None else "無線滑鼠"
    container, agent, model, old = _photo_card_to_change("滑鼠", "鍵盤")
    mouse, _ = _ops(old)
    container.put(_purchase_inventory(item_id="mouse", name=name, unit="個", aliases=aliases))

    response = _send_change(
        agent, model, old, {"operation_id": mouse["operation_id"], "quantity": 2}
    )

    assert response.results[0].data["code"] == "line_refused"
    assert response.results[0].data["lines"] == [
        {
            "operation_id": mouse["operation_id"],
            "reason": f"家裡現在已經有「{name}」，不會再新增一個。",
        }
    ]
    assert response.pending_approval.model_dump(mode="json") == old
    assert _stored(container) == [(old["approval_id"], "pending")]


# ----- Issue #188: the name and location of a photo line ----------------------------


def _line_values(op):
    return {k: v for k, v in op.values.items() if k != "transaction_id"}


def test_name_quantity_and_location_change_together_in_one_new_card():
    container, agent, model, old = _photo_card_to_change("張國周強胃散")
    (line,) = _ops(old)

    response = _send_change(
        agent,
        model,
        old,
        {"operation_id": line["operation_id"], "name": "胃散", "quantity": 2, "location": "藥箱"},
    )

    assert response.message == CARD_CHANGED
    new = response.pending_approval
    (new_line,) = new.operations
    assert _line_values(new_line) == {
        "name": "胃散",
        "quantity": 2,
        "unit": "個",
        "location": "藥箱",
    }
    assert new_line.description == "會新增一個庫存品項：胃散 2 個，放在藥箱（照片辨識）"
    assert _stored(container) == [
        (old["approval_id"], "cancelled"),
        (new.approval_id, "pending"),
    ]


@pytest.mark.parametrize(
    ("change", "name", "location"),
    [({"name": "胃散"}, "胃散", None), ({"location": "藥箱"}, "張國周強胃散", "藥箱")],
    ids=["name only", "location only"],
)
def test_the_name_or_the_location_changes_alone(change, name, location):
    _, agent, model, old = _photo_card_to_change("張國周強胃散", "滑鼠")
    stomach, mouse = _ops(old)

    new = _send_change(
        agent, model, old, {"operation_id": stomach["operation_id"], **change}
    ).pending_approval

    first, second = new.operations
    assert _line_values(first) == {
        "name": name,
        "quantity": 1,
        "unit": "個",
        "location": location,
    }
    assert _line_values(second) == {"name": "滑鼠", "quantity": 1, "unit": "個", "location": None}


def test_a_filled_in_location_changes_again_and_null_empties_it():
    _, agent, model, old = _photo_card_to_change("滑鼠")

    def changed(card, location):
        (line,) = _ops(card)
        response = _send_change(
            agent, model, card, {"operation_id": line["operation_id"], "location": location}
        )
        return response.pending_approval.model_dump(mode="json")

    desk = changed(old, "書桌")
    drawer = changed(desk, "抽屜")
    empty = changed(drawer, None)

    assert [_ops(card)[0]["values"]["location"] for card in (desk, drawer, empty)] == [
        "書桌",
        "抽屜",
        None,
    ]
    assert _ops(empty)[0]["description"] == "會新增一個庫存品項：滑鼠 1 個（照片辨識）"


@pytest.mark.parametrize("aliases", [None, ["胃散"]])
def test_a_new_name_the_household_has_is_refused(aliases):
    name = "胃散" if aliases is None else "腸胃藥"
    container, agent, model, old = _photo_card_to_change("張國周強胃散")
    (line,) = _ops(old)
    container.put(_purchase_inventory(item_id="stomach", name=name, unit="包", aliases=aliases))

    response = _send_change(
        agent, model, old, {"operation_id": line["operation_id"], "name": "胃散"}
    )

    assert response.results[0].data["code"] == "line_refused"
    assert response.results[0].data["lines"] == [
        {
            "operation_id": line["operation_id"],
            "reason": f"家裡現在已經有「{name}」，不會再新增一個。",
        }
    ]
    assert response.pending_approval.model_dump(mode="json") == old
    assert _stored(container) == [(old["approval_id"], "pending")]


def test_two_lines_with_the_same_name_are_refused():
    container, agent, model, old = _photo_card_to_change("滑鼠", "鍵盤")
    mouse, keyboard = _ops(old)

    response = _send_change(
        agent, model, old, {"operation_id": keyboard["operation_id"], "name": "滑鼠"}
    )

    reason = "同一張卡上有兩項都叫「滑鼠」，請改成不同的名字。"
    assert response.results[0].data["code"] == "line_refused"
    assert response.results[0].data["lines"] == [
        {"operation_id": mouse["operation_id"], "reason": reason},
        {"operation_id": keyboard["operation_id"], "reason": reason},
    ]
    assert _stored(container) == [(old["approval_id"], "pending")]


def test_with_photo_cards_off_a_photo_card_cannot_be_changed():
    """Off in the running app until photo items can be written: nothing to change there."""
    container, agent, model, old = _photo_card_to_change("滑鼠", photo_cards=False)
    (line,) = _ops(old)

    response = _send_change(
        agent, model, old, {"operation_id": line["operation_id"], "quantity": 2}
    )

    assert response.message == CHANGE_NOT_THIS_CARD
    assert response.results[0].data == {
        "code": "line_refused",
        "lines": [{"operation_id": line["operation_id"], "reason": CHANGE_NOT_THIS_LINE}],
    }
    assert response.pending_approval.model_dump(mode="json") == old
    assert _stored(container) == [(old["approval_id"], "pending")]
