"""A1 item-match card (contract section 10, Issue #175): "這是家裡的洗潔精嗎？".

A list entry not linked to an inventory item may be a household item under another
name. The card asks once: 「是」 writes the purchase into that item, as a purchase card
would, and remembers no alias; 「不是」 closes the card and writes nothing. The cards go
through the same `CardWriteRouter` as the running app (`get_approval_service`).
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_purchase_cards import (
    HOME,
    T0,
    FakeCosmosContainer,
    ScriptedModel,
    _call,
    agent,
    ask,
    inventory,
    purchase_reply,
    resume_app,
    shopping,
    tool_results,
)

from app.agent.cards import (
    CHANGE_NOT_THIS_CARD,
    ITEM_MATCH_MADE,
    MATCH_LATER,
    NO_SUCH_ITEM,
    NOT_SAME_ITEM_REPLY,
    NOT_SAME_ITEM_SAME_NAME,
    OTHER_UNIT,
    PurchaseCards,
    item_match_options,
)
from app.agent.read_agent import ReadAgent, load_system_prompt
from app.models.agent import AgentRequest, ApprovalPresentation, CardChange
from app.services.approvals import (
    CONFIRM_ITEM_MATCH,
    ApprovalService,
    NewOperation,
    NewOption,
)
from app.services.card_writes import CardWriteRouter
from app.services.cosmos_read import (
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
)
from app.services.cosmos_write import CosmosWriteRepository
from app.services.purchase_inputs import CardInputResolver
from app.services.shopping.workflow import ShoppingWriteBuilder
from app.tools.queries import QueryTools


def soap(quantity=1.0, unit="瓶", aliases=None):
    """洗潔精 at home: the item 洗碗精 on the list may be."""
    return inventory("dish-soap", "洗潔精", quantity, unit, aliases)


def dish(shopping_item_id="dish-1", name="洗碗精", unit="瓶"):
    """洗碗精 on the list, not linked to an inventory item."""
    return shopping(shopping_item_id, name, None, 1, unit)


def setup(*records):
    """The running app's builders: the router hands each card to the one that writes it."""
    container = FakeCosmosContainer()
    for record in records:
        container.put(record)
    reader = CosmosReadRepository(container)
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        CardWriteRouter(ShoppingWriteBuilder(container, CardInputResolver(reader))),
        clock=lambda: T0,
    )
    return container, reader, approvals


def match_card(reader, approvals, *items, message="洗碗精買回來了，1 瓶"):
    model = ScriptedModel(
        purchase_reply(*items, call_id="call-1"),
        AIMessage(content="請在下方卡片選「是」或「不是」。"),
    )
    return ask(agent(model, reader, approvals), message), model


def answer(approvals, card, **body):
    with TestClient(resume_app(approvals)) as client:
        return client.post(
            "/agent/resume",
            json={"household_id": HOME, "approval_id": card.approval_id, **body},
        )


def nothing_written(container, soap_quantity=1):
    assert container.get("inventory_item:dish-soap")["quantity"] == soap_quantity
    assert container.get("shopping_item:dish-1")["status"] == "pending"
    assert container.of_type("transaction") == []
    assert len(container.of_type("inventory_item")) == 1  # no new item either


def test_the_model_names_a_household_item_and_gets_an_item_match_card():
    container, reader, approvals = setup(soap(), dish())

    response, model = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.ITEM_MATCH
    (line,) = card.operations
    assert line.action == CONFIRM_ITEM_MATCH
    assert line.description == "這是家裡的洗潔精嗎？"
    assert {k: v for k, v in line.values.items() if k != "transaction_id"} == {
        "user_term": "洗碗精",
        "candidate_item_id": "dish-soap",
        "candidate_name": "洗潔精",
        "quantity": 1,
        "unit": "瓶",
        "shopping_item_id": "dish-1",
    }
    assert line.values["transaction_id"]
    assert card.options == [
        {"label": "是，加到洗潔精", "option_id": "same_item"},
        {"label": "不是，這是新的東西", "option_id": "new_item"},
    ]
    stored = container.get(f"pending_approval:{card.approval_id}")
    assert stored["status"] == "pending"
    assert {t["record_id"] for t in stored["operations"][0]["targets"]} == {
        "shopping_item:dish-1",
        "inventory_item:dish-soap",
    }
    nothing_written(container)
    result = json.loads(tool_results(model)[-1])
    assert result["card"] == {"item_match": "這是家裡的洗潔精嗎？"}
    assert result["note"] == ITEM_MATCH_MADE.format(term="洗碗精", candidate="洗潔精")


@pytest.mark.parametrize(
    "household, candidate",
    [
        (inventory("paste", "牙膏", 0, "條"), "牙膏"),  # same name, entry not linked
        (inventory("paste", "高露潔", 1, "條", aliases=["牙膏"]), "高露潔"),  # an alias
    ],
)
def test_one_household_item_with_the_entrys_name_or_alias_gets_an_item_match_card(
    household, candidate
):
    """Without a candidate from the model: before Issue #175 this was refused."""
    container, reader, approvals = setup(household, shopping("paste-1", "牙膏", None, 1, "條"))

    response, _ = match_card(
        reader, approvals, {"shopping_item_id": "paste-1", "quantity": 1}, message="牙膏買了 1 條"
    )

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.ITEM_MATCH
    assert card.operations[0].values["candidate_item_id"] == "paste"
    assert card.operations[0].description == f"這是家裡的{candidate}嗎？"
    assert container.of_type("transaction") == []


def test_yes_writes_the_purchase_into_that_item_once_and_remembers_no_alias():
    container, reader, approvals = setup(soap(), dish())
    card = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )[0].pending_approval

    first = answer(approvals, card, approved=True, selected_option_id="same_item")
    again = answer(approvals, card, approved=True, selected_option_id="same_item")  # double tap

    assert first.status_code == 200
    assert first.json()["message"] == "已記錄：洗潔精 +1 瓶（現在 2 瓶）"
    assert again.json()["message"] == first.json()["message"]
    stocked = container.get("inventory_item:dish-soap")
    assert stocked["quantity"] == 2
    assert not stocked.get("aliases")  # no alias is remembered (Issue #175)
    assert len(container.of_type("inventory_item")) == 1  # no new item
    assert container.get("shopping_item:dish-1")["status"] == "completed"
    (transaction,) = container.of_type("transaction")
    assert (transaction["item_id"], transaction["quantity_delta"]) == ("dish-soap", 1)
    assert transaction["transaction_id"] == card.operations[0].values["transaction_id"]
    stored = container.get(f"pending_approval:{card.approval_id}")
    assert (stored["status"], stored["selected_option_id"]) == ("completed", "same_item")


def test_no_closes_the_card_writes_nothing_and_asks_for_the_item_again():
    container, reader, approvals = setup(soap(), dish())
    card = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )[0].pending_approval

    first = answer(approvals, card, approved=True, selected_option_id="new_item")
    again = answer(approvals, card, approved=True, selected_option_id="new_item")

    words = NOT_SAME_ITEM_REPLY.format(term="洗碗精", candidate="洗潔精", quantity=1, unit="瓶")
    assert first.status_code == 200
    assert first.json()["message"] == words
    assert first.json()["pending_approval"] is None
    assert again.json()["message"] == words
    stored = container.get(f"pending_approval:{card.approval_id}")
    assert (stored["status"], stored["cancel_reason"]) == ("cancelled", "not_same_item")
    nothing_written(container)


def test_no_on_an_item_of_the_same_name_says_it_is_never_added_twice():
    container, reader, approvals = setup(
        inventory("paste", "牙膏", 0, "條"), shopping("paste-1", "牙膏", None, 1, "條")
    )
    card = match_card(
        reader, approvals, {"shopping_item_id": "paste-1", "quantity": 1}, message="牙膏買了 1 條"
    )[0].pending_approval

    response = answer(approvals, card, approved=True, selected_option_id="new_item")

    assert response.json()["message"] == NOT_SAME_ITEM_SAME_NAME.format(candidate="牙膏")
    assert container.of_type("transaction") == []
    assert len(container.of_type("inventory_item")) == 1


def test_cancel_writes_nothing():
    container, reader, approvals = setup(soap(), dish())
    card = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )[0].pending_approval

    response = answer(approvals, card, approved=False)

    assert response.json()["message"] == "已取消，沒有記錄任何東西。"
    nothing_written(container)


@pytest.mark.parametrize(
    "selection",
    [
        {},  # no option chosen
        {"selected_option_id": "o-1"},  # not on this card
        {"approved_operation_ids": ["x"]},  # a batch selection
    ],
)
def test_an_answer_that_is_not_yes_or_no_is_refused(selection):
    container, reader, approvals = setup(soap(), dish())
    card = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )[0].pending_approval

    response = answer(approvals, card, approved=True, **selection)

    assert response.status_code == 409
    nothing_written(container)


def test_yes_after_no_or_no_after_yes_keeps_the_first_answer():
    container, reader, approvals = setup(soap(), dish())
    said = {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"}
    closed = match_card(reader, approvals, said)[0].pending_approval

    answer(approvals, closed, approved=True, selected_option_id="new_item")
    late_yes = answer(approvals, closed, approved=True, selected_option_id="same_item")

    assert "沒有記錄任何東西" in late_yes.json()["message"]
    nothing_written(container)


def test_a_record_changed_after_the_card_asks_to_say_it_again():
    container, reader, approvals = setup(soap(), dish())
    card = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )[0].pending_approval
    container.put(soap(quantity=5))

    response = answer(approvals, card, approved=True, selected_option_id="same_item")

    assert "剛剛被改過" in response.json()["message"]
    nothing_written(container, soap_quantity=5)


def test_an_unsure_item_with_other_items_is_said_again_after_their_card():
    """Issue #175 point 2: the sure items get the card; the unsure one comes later."""
    container, reader, approvals = setup(inventory(), shopping(), soap(), dish())

    response, model = match_card(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2},
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
        message="衛生紙 2 包和洗碗精 1 瓶買回來了",
    )

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.SINGLE_ACTION
    assert [line.values["shopping_item_id"] for line in card.operations] == ["tissue-1"]
    assert json.loads(tool_results(model)[-1])["not_on_card"] == [
        {"shopping_item_id": "dish-1", "reason": MATCH_LATER.format(candidate="洗潔精")}
    ]


def test_two_unsure_items_get_one_item_match_card_and_the_other_comes_later():
    container, reader, approvals = setup(
        soap(),
        inventory("bath", "沐浴乳", 1, "瓶"),
        dish(),
        shopping("gel-1", "沐浴精", None, 1, "瓶"),
    )

    response, model = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
        {"shopping_item_id": "gel-1", "quantity": 1, "candidate_item_id": "bath"},
        message="洗碗精和沐浴精各 1 瓶買回來了",
    )

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.ITEM_MATCH
    assert card.operations[0].values["shopping_item_id"] == "dish-1"
    assert json.loads(tool_results(model)[-1])["not_on_card"] == [
        {"shopping_item_id": "gel-1", "reason": MATCH_LATER.format(candidate="沐浴乳")}
    ]


@pytest.mark.parametrize(
    "household, said, reason",
    [
        ([soap()], {"candidate_item_id": "nope"}, NO_SUCH_ITEM),
        ([soap(unit="罐")], {"candidate_item_id": "dish-soap"}, OTHER_UNIT),
        ([soap()], {"candidate_item_id": "dish-soap", "unit": "包"}, OTHER_UNIT),
    ],
)
def test_no_item_match_card_for_an_unknown_item_or_another_unit(household, said, reason):
    container, reader, approvals = setup(*household, dish())

    response, model = match_card(
        reader, approvals, {"shopping_item_id": "dish-1", "quantity": 1, **said}
    )

    assert response.pending_approval is None
    assert json.loads(tool_results(model)[-1]) == {
        "card": None,
        "not_on_card": [{"shopping_item_id": "dish-1", "reason": reason}],
    }
    assert container.of_type("pending_approval") == []


def test_a_linked_entry_ignores_a_candidate_and_gets_a_purchase_card():
    container, reader, approvals = setup(inventory(), shopping(), soap())

    response, _ = match_card(
        reader,
        approvals,
        {"shopping_item_id": "tissue-1", "quantity": 2, "candidate_item_id": "dish-soap"},
        message="我買了衛生紙 2 包",
    )

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.SINGLE_ACTION
    assert card.operations[0].values["item_id"] == "tissue"


def test_an_item_match_card_cannot_be_changed_on_the_card():
    container, reader, approvals = setup(soap(), dish())
    card = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )[0].pending_approval
    tools = QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader))
    the_agent = ReadAgent(
        ScriptedModel(), tools, load_system_prompt(), purchases=PurchaseCards(reader, approvals)
    )
    response = asyncio.run(
        the_agent.respond(
            AgentRequest(
                household_id=HOME,
                message="送出修改",
                replaces_approval_id=card.approval_id,
                card_changes=[CardChange(operation_id=card.operations[0].operation_id, quantity=2)],
            )
        )
    )

    assert response.results[0].data["code"] == "line_refused"
    assert CHANGE_NOT_THIS_CARD in response.message
    assert container.get(f"pending_approval:{card.approval_id}")["status"] == "pending"


def test_the_open_item_match_card_comes_back_with_the_next_message():
    container, reader, approvals = setup(soap(), dish())
    card = match_card(
        reader,
        approvals,
        {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
    )[0].pending_approval
    model = ScriptedModel(AIMessage(content="上一張卡還沒回答，請先選「是」或「不是」。"))

    response = ask(agent(model, reader, approvals), "家裡還有洗潔精嗎？")

    assert response.pending_approval.approval_id == card.approval_id
    assert response.pending_approval.presentation is ApprovalPresentation.ITEM_MATCH


def line(**values):
    return NewOperation(
        action=CONFIRM_ITEM_MATCH, description="這是家裡的洗潔精嗎？", values=values
    )


@pytest.mark.parametrize(
    "operations, options",
    [
        ([line(), line()], item_match_options("洗潔精")),  # two lines
        (
            [NewOperation(action="complete_purchase", description="x", values={})],
            item_match_options("洗潔精"),
        ),
        ([line()], []),  # no options
        ([line()], list(reversed(item_match_options("洗潔精")))),  # wrong order
        ([line()], [NewOption("same_item", {}), NewOption("other", {})]),
        (
            [line()],
            [NewOption("same_item", {"option_id": "x"}), NewOption("new_item", {})],
        ),
    ],
)
def test_the_service_refuses_an_item_match_card_of_any_other_shape(operations, options):
    _, _, approvals = setup()

    with pytest.raises(ValueError):
        approvals.prepare(
            household_id=HOME,
            presentation=ApprovalPresentation.ITEM_MATCH,
            operations=operations,
            options=options,
        )


def test_the_model_is_told_about_candidate_item_id():
    model = ScriptedModel(AIMessage(content="好"))
    _, reader, approvals = setup()
    agent(model, reader, approvals)

    (purchase,) = [t for t in model.bound_tools if t["function"]["name"] == "complete_purchase"]
    item = purchase["function"]["parameters"]["properties"]["items"]["items"]
    assert "candidate_item_id" in item["properties"]
    assert "candidate_item_id" not in item["required"]
    assert "candidate_item_id" in load_system_prompt()


def test_list_then_purchase_reaches_the_item_match_card():
    """The model's two steps as the prompt asks: list, inventory, then the card."""
    container, reader, approvals = setup(soap(), dish())
    model = ScriptedModel(
        _call("list_shopping"),
        _call("list_inventory", "call-2"),
        purchase_reply(
            {"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "dish-soap"},
            call_id="call-3",
        ),
        AIMessage(content="請在下方卡片選「是」或「不是」。"),
    )

    response = ask(agent(model, reader, approvals), "洗碗精買回來了，1 瓶")

    assert response.pending_approval.presentation is ApprovalPresentation.ITEM_MATCH
