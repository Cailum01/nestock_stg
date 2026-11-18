"""A1: card lines that add to an item the household has, with no list entry (Issue #206).

「我又收到一個滑鼠」 or a photo of something the household has: with `StockIncreases`
on, that item gets an `increase_stock` line on the card instead of being left out.
A1's part only: the card, and `CardWriteRouter` handing it to the builder for these
lines. That builder is B1's and C1's; here `_Writer` stands in for it, and the app's
own composition uses the real writer and planner.
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_photo_end_to_end import _app_approvals, _confirm
from test_purchase_cards import (
    HOME,
    FakeCosmosContainer,
    ScriptedModel,
    _call,
    inventory,
    shopping,
    tool_results,
)
from test_read_agent import PHOTO, _ask_photo, _detected, _seen_by_model, _StubVision

from app.agent.cards import (
    CARD_FAILED,
    CHANGE_NOT_THIS_CARD,
    PHOTO_CARD_MADE,
    PHOTO_SEEN,
    SAID,
    PhotoCards,
    PurchaseCards,
)
from app.agent.read_agent import ReadAgent, load_system_prompt, stock_increase_on
from app.agent.stock_cards import (
    ADD_STOCK,
    HAVE_IT,
    INCREASE_CARD_NOTE,
    INCREASE_PROMPT_NOTE,
    INCREASE_TOOL_DESCRIPTION,
    SAID_TWICE,
    STOCK_CARD_MADE,
    STOCK_TOOL_SCHEMA,
    UNIT_DIFFERS,
    WHICH_ONE,
    StockCards,
    StockIncreases,
)
from app.models.agent import AgentRequest, ApprovalPresentation, CardChange
from app.services.approvals import ApprovalService, DomainWritePlan
from app.services.card_writes import (
    INCREASE_STOCK_NOT_SUPPORTED,
    INCREASE_STOCK_WRITE_MISSING,
    CardWriteRouter,
)
from app.services.cosmos_read import (
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
)
from app.services.cosmos_write import CosmosWriteRepository
from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage
from app.services.purchase_inputs import INCREASE_STOCK, UnsupportedAction
from app.tools.queries import QueryTools

MOUSE = inventory(item_id="mouse", name="羅技 G400s 有線滑鼠", quantity=1.0, unit="個")
SOY_A = inventory(item_id="soy-1", name="無糖豆漿", quantity=2.0, unit="瓶", aliases=["豆漿"])
SOY_B = inventory(item_id="soy-2", name="高纖豆漿", quantity=1.0, unit="瓶", aliases=["豆漿"])


class _Writer:
    """Stands in for B1's no-Shopping writer (PR #208): plans every line, writes nothing.

    `stock_cards` are the cards with an `increase_stock` line it was handed, each with
    the reader the router passed for the item's current version.
    """

    def __init__(self) -> None:
        self.cards = []
        self.stock_cards = []

    async def build(self, record):
        self.cards.append(record)
        return _plan(record)

    async def build_stock_card(self, record, inventory_reader):
        self.stock_cards.append((record, inventory_reader))
        return _plan(record)


class _NewItemsOnly:
    """The no-Shopping writer on main today: no `build_stock_card`."""

    async def build(self, record):
        return _plan(record)


def _plan(record):
    return DomainWritePlan(operations_by_id={op: () for op in record.approved_operation_ids})


class _Never:
    """A builder that must not be reached."""

    async def build(self, record):
        raise AssertionError(f"{type(self).__name__} got card {record.approval_id}")


def home(*records, writer=True):
    """(container, reads, approvals, writer). With `writer`, increase cards can be stored."""
    container = FakeCosmosContainer()
    for record in records or (MOUSE,):
        container.put(record)
    reads = CosmosReadRepository(container)
    photo_items = _Writer() if writer else _NewItemsOnly()
    router = CardWriteRouter(_Never(), photo_items=photo_items, household_items=reads)
    approvals = ApprovalService(CosmosWriteRepository(container, query_client=container), router)
    return container, reads, approvals, photo_items if writer else None


def agent(reads, approvals, *replies, on=True, vision=None):
    increases = StockIncreases(reads) if on else None
    media = None
    if vision is not None:
        media = LocalStagingMediaStorage()
        asyncio.run(media.simulate_client_upload(HOME, PHOTO, b"jpeg-bytes"))
    return ReadAgent(
        ScriptedModel(*replies),
        QueryTools(CosmosInventoryService(reads), CosmosShoppingService(reads)),
        load_system_prompt(),
        vision=vision,
        media=media,
        purchases=PurchaseCards(reads, approvals),
        photo_cards=PhotoCards(approvals),
        stock=StockCards(reads, approvals, increases),
        increases=increases,
    )


def said(reads, approvals, *items, on=True):
    the_agent = agent(
        reads,
        approvals,
        _call(ADD_STOCK, "call-1", items=list(items)),
        AIMessage(content="請確認下方的卡片，按確認後才會記錄。"),
        on=on,
    )
    request = AgentRequest(household_id=HOME, message="我又收到一個滑鼠")
    response = asyncio.run(the_agent.respond(request))
    return response, json.loads(tool_results(the_agent._model)[-1]), the_agent


def one_more(name="羅技 G400s 有線滑鼠", quantity=1, unit="個"):
    return {"name": name, "quantity": quantity, "unit": unit}


def test_a_thing_the_household_has_gets_a_line_that_adds_to_it():
    container, reads, approvals, writer = home()
    before = container.get("inventory_item:mouse")

    response, result, _ = said(reads, approvals, one_more(quantity=2))

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.SINGLE_ACTION
    (line,) = card.operations
    assert line.action == INCREASE_STOCK
    assert line.description == f"羅技 G400s 有線滑鼠 +2 個（家裡已有的品項）{SAID}"
    assert {k: v for k, v in line.values.items() if k != "transaction_id"} == {
        "item_id": "mouse",
        "name": "羅技 G400s 有線滑鼠",
        "quantity_added": 2,
        "unit": "個",
        "reason": "adjustment",  # fixed in this version (Issue #206, C2 and B2)
        "inventory_quantity_before": 1,  # the quantity in the same read as the ETag
    }
    assert line.values["transaction_id"]
    stored = container.get(f"pending_approval:{card.approval_id}")
    assert stored["status"] == "pending"
    # The card holds the item at the version it was read with.
    assert stored["operations"][0]["targets"] == [
        {"record_id": "inventory_item:mouse", "etag": before["_etag"]}
    ]
    assert container.get("inventory_item:mouse") == before  # nothing written but the card
    assert container.of_type("transaction") == []
    assert result == {
        "card": {"lines": [line.description]},
        "not_on_card": [],
        "note": INCREASE_CARD_NOTE,
    }
    # The save-time check went to `build_stock_card`, with the reader for the item.
    ((checked, reader),) = writer.stock_cards
    assert (checked.approval_id, reader) == (card.approval_id, reads)
    assert writer.cards == []
    assert type(line.values["inventory_quantity_before"]) is int


def test_an_alias_adds_to_the_item_under_its_own_name():
    aliased = inventory(item_id="mouse", name="羅技 G400s 有線滑鼠", unit="個", aliases=["滑鼠"])
    _, reads, approvals, _ = home(aliased)

    response, _, _ = said(reads, approvals, one_more(name="滑鼠"))

    (line,) = response.pending_approval.operations
    assert (line.values["item_id"], line.values["name"]) == ("mouse", "羅技 G400s 有線滑鼠")


def test_off_a_thing_the_household_has_is_left_out_as_before():
    container, reads, approvals, _ = home()

    response, result, the_agent = said(reads, approvals, one_more(), on=False)

    assert response.pending_approval is None
    assert result == {
        "card": None,
        "not_on_card": [
            {"name": "羅技 G400s 有線滑鼠", "reason": HAVE_IT.format(names="羅技 G400s 有線滑鼠")}
        ],
    }
    assert container.of_type("pending_approval") == []
    assert INCREASE_PROMPT_NOTE not in [m.content for m in the_agent._model.seen[0]]
    assert the_agent._stock.tool_schema is STOCK_TOOL_SCHEMA


def test_on_the_model_is_told_it_may_add_to_household_items():
    _, reads, approvals, _ = home()

    _, _, the_agent = said(reads, approvals, one_more())

    assert INCREASE_PROMPT_NOTE in [m.content for m in the_agent._model.seen[0]]
    schema = the_agent._stock.tool_schema
    assert schema["function"]["description"] == INCREASE_TOOL_DESCRIPTION
    assert schema["function"]["parameters"] == STOCK_TOOL_SCHEMA["function"]["parameters"]
    assert schema["function"]["name"] == ADD_STOCK


def test_a_new_thing_and_a_household_item_share_one_card():
    _, reads, approvals, writer = home()
    shampoo = {"name": "洗髮精", "quantity": 1, "unit": "瓶", "location": "浴室"}

    response, result, _ = said(reads, approvals, shampoo, one_more())

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.BATCH_ACTION
    assert [op.action for op in card.operations] == ["create_item", INCREASE_STOCK]
    assert result["note"] == STOCK_CARD_MADE + INCREASE_CARD_NOTE
    assert len(writer.stock_cards) == 1  # the whole card goes to one builder
    assert writer.cards == []


def test_another_unit_is_asked_again_never_converted():
    container, reads, approvals, _ = home()

    response, result, _ = said(reads, approvals, one_more(unit="盒"))

    assert response.pending_approval is None
    assert result["card"] is None
    assert result["not_on_card"] == [
        {
            "name": "羅技 G400s 有線滑鼠",
            "reason": UNIT_DIFFERS.format(name="羅技 G400s 有線滑鼠", unit="個", said="盒"),
        }
    ]
    assert container.of_type("pending_approval") == []


def test_several_household_items_of_that_name_are_asked_which():
    _, reads, approvals, _ = home(SOY_A, SOY_B)

    response, result, _ = said(reads, approvals, one_more(name="豆漿", unit="瓶"))

    assert response.pending_approval is None
    (left,) = result["not_on_card"]
    assert left["reason"] == WHICH_ONE.format(names="無糖豆漿、高纖豆漿")


def test_one_item_said_under_two_names_gets_one_line():
    aliased = inventory(item_id="mouse", name="羅技 G400s 有線滑鼠", unit="個", aliases=["滑鼠"])
    _, reads, approvals, _ = home(aliased)

    response, result, _ = said(reads, approvals, one_more(), one_more(name="滑鼠", quantity=5))

    (line,) = response.pending_approval.operations
    assert line.values["quantity_added"] == 1  # the first one said
    assert result["not_on_card"] == [{"name": "滑鼠", "reason": SAID_TWICE}]


def test_without_its_writer_no_such_card_is_stored():
    container, reads, approvals, _ = home(writer=False)

    response, result, _ = said(reads, approvals, one_more())

    assert response.pending_approval is None
    assert result == json.loads(CARD_FAILED)
    assert container.of_type("pending_approval") == []


def test_the_apps_own_composition_stores_card_without_writing_inventory():
    """The app stores the approval; domain writes wait for the user's confirmation."""
    container = FakeCosmosContainer()
    for record in (MOUSE, shopping()):
        container.put(record)
    reads, approvals = _app_approvals(container)

    response, result, _ = said(reads, approvals, one_more())

    card = response.pending_approval
    assert card is not None
    assert result["note"] == INCREASE_CARD_NOTE
    (stored_card,) = container.of_type("pending_approval")
    assert stored_card["status"] == "pending"
    (stored_item,) = container.of_type("inventory_item")
    assert stored_item["quantity"] == MOUSE.quantity
    assert container.of_type("transaction") == []


def _increase_card(reads, approvals):
    line = asyncio.run(
        StockIncreases(reads).line(
            HOME, _match(reads, "羅技 G400s 有線滑鼠"), quantity=1, unit="個", source=SAID
        )
    )[0]
    return approvals.prepare(
        household_id=HOME, presentation=ApprovalPresentation.SINGLE_ACTION, operations=[line]
    )


@pytest.mark.parametrize(
    ("photo_items", "message"),
    [
        (None, INCREASE_STOCK_WRITE_MISSING),
        (_NewItemsOnly(), INCREASE_STOCK_NOT_SUPPORTED),
    ],
    ids=["no writer", "a writer for new items only"],
)
def test_the_router_refuses_such_a_card_without_a_writer_for_it(photo_items, message):
    """And never hands it to the purchase builder (`_Never`)."""
    _, reads, approvals, _ = home(writer=False)
    record = _increase_card(reads, approvals)

    with pytest.raises(UnsupportedAction) as refused:
        asyncio.run(CardWriteRouter(_Never(), photo_items=photo_items).build(record))
    assert str(refused.value) == message


def _match(reads, name):
    from app.services.item_matching import match_term

    return match_term(name, asyncio.run(reads.list_inventory(HOME)))


def test_confirming_goes_to_the_writer_and_an_added_item_is_not_a_duplicate():
    """The household has the item of an increase line: the still-new check skips it."""
    _, reads, approvals, writer = home()
    card = said(reads, approvals, one_more())[0].pending_approval

    _confirm(approvals, card)  # `_Writer` plans no write, so nothing is recorded here

    handed = [record for record, _ in writer.stock_cards]
    assert [c.approval_id for c in handed] == [card.approval_id] * 2  # save, confirm
    assert tuple(handed[-1].approved_operation_ids) == (card.operations[0].operation_id,)


def test_cancelling_writes_nothing():
    container, reads, approvals, _ = home()
    before = container.get("inventory_item:mouse")
    card = said(reads, approvals, one_more())[0].pending_approval
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": False}
    from fastapi.testclient import TestClient
    from test_purchase_cards import resume_app

    with TestClient(resume_app(approvals)) as client:
        reply = client.post("/agent/resume", json=body).json()

    assert reply["results"][0]["data"]["outcome"] == "cancelled"
    assert container.get("inventory_item:mouse") == before
    assert container.get(f"pending_approval:{card.approval_id}")["status"] == "cancelled"


def test_the_quantity_cannot_be_changed_on_the_card_yet():
    container, reads, approvals, _ = home()
    response, _, the_agent = said(reads, approvals, one_more())
    card = response.pending_approval
    change = CardChange(operation_id=card.operations[0].operation_id, quantity=3)
    request = AgentRequest(
        household_id=HOME,
        message="送出修改",
        replaces_approval_id=card.approval_id,
        card_changes=[change],
    )

    reply = asyncio.run(the_agent.respond(request))

    assert reply.results[0].data["code"] == "line_refused"
    assert reply.message == CHANGE_NOT_THIS_CARD
    assert reply.pending_approval.approval_id == card.approval_id  # the old card stays
    assert len(container.of_type("pending_approval")) == 1


def test_a_card_with_new_and_household_items_cannot_be_changed_on_the_card():
    """Issue #206 (C2): a mixed card is only ticked and confirmed or cancelled."""
    container, reads, approvals, _ = home()
    shampoo = {"name": "洗髮精", "quantity": 1, "unit": "瓶"}
    response, _, the_agent = said(reads, approvals, shampoo, one_more())
    card = response.pending_approval
    change = CardChange(operation_id=card.operations[0].operation_id, location="浴室")
    request = AgentRequest(
        household_id=HOME,
        message="送出修改",
        replaces_approval_id=card.approval_id,
        card_changes=[change],
    )

    reply = asyncio.run(the_agent.respond(request))

    assert reply.results[0].data["code"] == "line_refused"
    assert reply.pending_approval.approval_id == card.approval_id
    assert len(container.of_type("pending_approval")) == 1


def test_the_added_quantity_is_what_was_said_or_seen_not_always_one():
    _, reads, approvals, _ = home()

    response, seen = _photo(
        reads, approvals, _detected("羅技 G400s 有線滑鼠", unit="個", quantity=3)
    )

    (line,) = response.pending_approval.operations
    assert line.values["quantity_added"] == 3
    assert line.values["inventory_quantity_before"] == 1
    assert line.description == f"羅技 G400s 有線滑鼠 +3 個（家裡已有的品項）{PHOTO_SEEN}"


def _photo(reads, approvals, *detected, on=True):
    the_agent = agent(
        reads,
        approvals,
        _call("analyze_photo"),
        AIMessage(content="請確認。"),
        on=on,
        vision=_StubVision(*detected),
    )
    response = _ask_photo(the_agent, household_id=HOME)
    return response, _seen_by_model(the_agent._model)


def test_a_photo_of_a_household_item_gets_a_line_that_adds_to_it():
    container, reads, approvals, _ = home()
    before = container.get("inventory_item:mouse")

    response, seen = _photo(reads, approvals, _detected("羅技 G400s 有線滑鼠", unit="個"))

    card = response.pending_approval
    (line,) = card.operations
    assert line.action == INCREASE_STOCK
    assert line.description == f"羅技 G400s 有線滑鼠 +1 個（家裡已有的品項）{PHOTO_SEEN}"
    assert line.values["item_id"] == "mouse"
    assert "image_ref" not in line.values  # an increase line keeps no photo
    assert seen["stock_increase_card"] == {
        "lines": [line.description],
        "note": INCREASE_CARD_NOTE,
    }
    assert "new_item_card" not in seen
    assert container.get("inventory_item:mouse") == before


def test_a_photo_with_new_and_household_items_makes_one_card():
    _, reads, approvals, _ = home()

    response, seen = _photo(
        reads,
        approvals,
        _detected("洗髮精"),
        _detected("羅技 G400s 有線滑鼠", unit="個"),
    )

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.BATCH_ACTION
    assert [op.action for op in card.operations] == ["create_item", INCREASE_STOCK]
    assert seen["new_item_card"]["note"] == PHOTO_CARD_MADE + INCREASE_CARD_NOTE
    assert seen["new_item_card"]["lines"] == [op.description for op in card.operations]


def test_a_photo_of_a_thing_with_several_household_items_gets_no_line():
    """Issue #206: which of them it is has to be settled before a card, not on it."""
    container, reads, approvals, _ = home(SOY_A, SOY_B)

    response, seen = _photo(reads, approvals, _detected("豆漿"))

    assert response.pending_approval is None
    assert "stock_increase_card" not in seen
    assert container.of_type("pending_approval") == []


def test_a_household_item_the_photo_shows_twice_gets_no_line():
    """PR #207 B1 review: adding only one of the sightings would record the wrong quantity."""
    container, reads, approvals, _ = home()
    mouse = _detected("羅技 G400s 有線滑鼠", unit="個")

    response, seen = _photo(reads, approvals, mouse, mouse)

    assert response.pending_approval is None
    assert "stock_increase_card" not in seen
    assert container.of_type("pending_approval") == []


def test_other_items_in_that_photo_still_get_their_lines():
    _, reads, approvals, _ = home(MOUSE, SOY_A)
    mouse = _detected("羅技 G400s 有線滑鼠", unit="個")

    response, _ = _photo(reads, approvals, mouse, _detected("無糖豆漿"), mouse)

    (line,) = response.pending_approval.operations
    assert line.values["item_id"] == "soy-1"


@pytest.mark.parametrize(
    "detected",
    [
        _detected("羅技 G400s 有線滑鼠", unit="盒"),
        _detected("羅技 G400s 有線滑鼠", unit="個", confidence=0.3),
        _detected("羅技 G400s 有線滑鼠", unit="個", unit_confirmed=False),
    ],
    ids=["another unit", "unsure", "unit not confirmed"],
)
def test_a_photo_item_that_cannot_be_added_is_only_reported(detected):
    container, reads, approvals, _ = home()

    response, seen = _photo(reads, approvals, detected)

    assert response.pending_approval is None
    assert "stock_increase_card" not in seen
    assert container.of_type("pending_approval") == []


def test_off_a_photo_of_a_household_item_is_only_reported_as_before():
    container, reads, approvals, _ = home()

    response, seen = _photo(reads, approvals, _detected("羅技 G400s 有線滑鼠", unit="個"), on=False)

    assert response.pending_approval is None
    assert "stock_increase_card" not in seen
    assert container.of_type("pending_approval") == []


@pytest.mark.parametrize(
    ("environment", "on"),
    [
        ({}, True),
        ({"NESTOCK_STOCK_INCREASE": ""}, True),
        ({"NESTOCK_STOCK_INCREASE": "1"}, True),
        ({"NESTOCK_STOCK_INCREASE": "0"}, False),
        ({"NESTOCK_STOCK_INCREASE": " 0 "}, False),
        ({"NESTOCK_STOCK_INCREASE": "false"}, True),
    ],
    ids=["unset", "empty", "1", "0", "0 with spaces", "another word"],
)
def test_the_switch_is_on_unless_set_to_0(environment, on):
    assert stock_increase_on(environment) is on


@pytest.mark.parametrize(("value", "on"), [(None, True), ("0", False)], ids=["unset", "0"])
def test_the_running_app_adds_to_household_items_unless_switched_off(monkeypatch, value, on):
    from app.agent import read_agent

    _, reads, _, _ = home()
    monkeypatch.setattr(read_agent, "get_cosmos_read_repository", lambda: reads)
    if value is None:
        monkeypatch.delenv("NESTOCK_STOCK_INCREASE", raising=False)
    else:
        monkeypatch.setenv("NESTOCK_STOCK_INCREASE", value)

    assert isinstance(read_agent._stock_increases(), StockIncreases) is on


def test_without_cosmos_the_running_app_never_adds_to_household_items(monkeypatch):
    from app.agent import read_agent

    monkeypatch.setattr(read_agent, "get_cosmos_read_repository", lambda: None)
    monkeypatch.delenv("NESTOCK_STOCK_INCREASE", raising=False)

    assert read_agent._stock_increases() is None
