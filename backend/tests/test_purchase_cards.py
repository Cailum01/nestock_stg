"""A1 v8 flow C end to end: the Agent makes a purchase card, `/agent/resume` writes it.

A scripted model stands in for Azure OpenAI. B1's read and write repositories run on
a faked Cosmos container that gives every write a new `_etag`. The approval service,
A1's card resolver and C1's write builder are the real ones.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.cards import (
    ALREADY_HAVE,
    MORE_THAN_LISTED,
    NEEDS_LOCATION,
    NEEDS_UNIT,
    OTHER_UNIT,
    PurchaseCards,
    purchase_operation,
    resume_response,
)
from app.agent.read_agent import ReadAgent, get_read_agent, load_system_prompt
from app.core import dependencies
from app.core.dependencies import (
    _create_approval_service,
    _create_cosmos_composition,
    get_approval_service,
)
from app.main import create_app
from app.models.agent import AgentRequest, ApprovalPresentation
from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus
from app.services.approvals import ApprovalService, NewOperation, ResumeOutcome, ResumeResult
from app.services.cosmos_read import (
    CosmosDocumentMapper,
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
)
from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    ReplaceDocument,
)
from app.services.inventory_item_identity import resolve_inventory_item_id
from app.services.purchase_inputs import (
    COMPLETE_PURCHASE,
    CREATE_ITEM,
    CardInputResolver,
    UnsupportedAction,
)
from app.services.shopping.preview import ShoppingDecisionRequired
from app.services.shopping.workflow import ShoppingWriteBuilder
from app.tools.queries import QueryTools

HOME = "demo-household"
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
CREATED = datetime(2026, 11, 1, tzinfo=timezone.utc)


class FakeCosmosContainer:
    """One container: batch, point read and query; each write gets a new ETag."""

    def __init__(self):
        self.documents = {}
        self._version = 0

    def put(self, record):
        document = CosmosDocumentMapper.to_document(record)
        self._version += 1
        self.documents[(record.household_id, document["id"])] = {
            **document,
            "_etag": f'"etag-{self._version}"',
        }

    def get(self, document_id, household_id=HOME):
        return self.documents.get((household_id, document_id))

    def of_type(self, record_type):
        return [doc for doc in self.documents.values() if doc.get("record_type") == record_type]

    async def execute_batch(self, *, partition_key, operations):
        await asyncio.sleep(0)
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


class ScriptedModel:
    """Stands in for Azure OpenAI: returns prepared replies and records what it saw."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def _call(name, call_id="call-1", **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def inventory(item_id="tissue", name="衛生紙", quantity=1.0, unit="包", aliases=None):
    return InventoryItem(
        item_id=item_id,
        household_id=HOME,
        name=name,
        quantity=quantity,
        unit=unit,
        aliases=aliases,
        created_at=CREATED,
        updated_at=CREATED,
    )


def shopping(shopping_item_id="tissue-1", name="衛生紙", item_id="tissue", quantity=2.0, unit="包"):
    return ShoppingItem(
        shopping_item_id=shopping_item_id,
        household_id=HOME,
        name=name,
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
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container),
        ShoppingWriteBuilder(container, CardInputResolver(reader)),
        clock=lambda: T0,
    )
    return container, reader, approvals


def agent(model, reader, approvals):
    tools = QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader))
    return ReadAgent(model, tools, load_system_prompt(), purchases=PurchaseCards(reader, approvals))


def ask(the_agent, message="我買了衛生紙 2 包", household_id=HOME):
    return asyncio.run(the_agent.respond(AgentRequest(household_id=household_id, message=message)))


def purchase_reply(*items, call_id="call-2"):
    return _call("complete_purchase", call_id, items=list(items))


def tool_results(model):
    return [m.content for m in model.seen[-1] if isinstance(m, ToolMessage)]


def test_bought_item_gets_a_card_and_nothing_else_changes():
    container, reader, approvals = setup()
    model = ScriptedModel(
        _call("list_shopping"),
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 2}),
        AIMessage(content="請確認下方的卡片，按確認後才會記錄。"),
    )

    response = ask(agent(model, reader, approvals))

    card = response.pending_approval
    assert card is not None
    assert card.presentation is ApprovalPresentation.SINGLE_ACTION
    assert card.operations[0].action == "complete_purchase"
    assert card.operations[0].description == "衛生紙 +2 包（從待購清單完成購買）"
    assert container.get(f"pending_approval:{card.approval_id}")["status"] == "pending"
    assert container.get("inventory_item:tissue")["quantity"] == 1
    assert container.get("shopping_item:tissue-1")["status"] == "pending"
    assert container.of_type("transaction") == []
    assert json.loads(tool_results(model)[-1])["card"]["lines"] == [
        "衛生紙 +2 包（從待購清單完成購買）"
    ]


def test_card_line_holds_what_the_user_sees_and_both_versions():
    """The line format A1's `CardInputResolver` reads back at confirmation."""
    container, reader, _ = setup()
    listed = asyncio.run(
        reader.read_shopping_item_with_etag(household_id=HOME, shopping_item_id="tissue-1")
    )
    stocked = asyncio.run(reader.read_inventory_item_with_etag(household_id=HOME, item_id="tissue"))

    operation = purchase_operation(shopping=listed, inventory=stocked, quantity=2)

    assert operation.action == COMPLETE_PURCHASE
    assert operation.description == "衛生紙 +2 包（從待購清單完成購買）"
    assert {k: v for k, v in operation.values.items() if k != "transaction_id"} == {
        "name": "衛生紙",
        "purchased_quantity": 2,
        "purchased_unit": "包",
        "shopping_item_id": "tissue-1",
        "item_id": "tissue",
    }
    assert operation.values["transaction_id"]
    assert {(t.record_id, t.etag) for t in operation.targets} == {
        ("shopping_item:tissue-1", container.get("shopping_item:tissue-1")["_etag"]),
        ("inventory_item:tissue", container.get("inventory_item:tissue")["_etag"]),
    }


@pytest.mark.parametrize(
    "listed, quantity",
    [
        (shopping(quantity=2), 3),  # more than listed
        (shopping(item_id=None), 1),  # not linked to an inventory item (Issue #90)
        (shopping(unit="串"), 1),  # a different unit
    ],
)
def test_purchases_c1_would_refuse_never_reach_a_card_line(listed, quantity):
    _, reader, _ = setup(inventory(), listed)
    entry = asyncio.run(
        reader.read_shopping_item_with_etag(household_id=HOME, shopping_item_id="tissue-1")
    )
    stocked = asyncio.run(reader.read_inventory_item_with_etag(household_id=HOME, item_id="tissue"))

    with pytest.raises(ShoppingDecisionRequired):
        purchase_operation(shopping=entry, inventory=stocked, quantity=quantity)


def test_a_card_with_other_actions_is_not_stored():
    container, _, approvals = setup()
    other = NewOperation(action="add_to_shopping_list", description="牛奶", values={})

    with pytest.raises(UnsupportedAction):
        asyncio.run(
            approvals.save(
                approvals.prepare(
                    household_id=HOME,
                    presentation=ApprovalPresentation.SINGLE_ACTION,
                    operations=[other],
                )
            )
        )
    assert container.of_type("pending_approval") == []


def test_several_items_go_on_one_card_and_unfit_ones_are_left_off_with_a_reason():
    container, reader, approvals = setup(
        inventory(),
        inventory("milk", "牛奶", 0, "瓶"),
        shopping(),
        shopping("milk-1", "牛奶", "milk", 1, "瓶"),
        shopping("dish-1", "洗碗精", None, 1, "瓶"),  # new to the household, no location said
    )
    model = ScriptedModel(
        purchase_reply(
            {"shopping_item_id": "tissue-1", "quantity": 2},
            {"shopping_item_id": "milk-1", "quantity": 1},
            {"shopping_item_id": "dish-1", "quantity": 1},
            call_id="call-1",
        ),
        AIMessage(content="衛生紙和牛奶請確認；洗碗精要放在哪裡？"),
    )

    response = ask(agent(model, reader, approvals))

    assert response.pending_approval.presentation is ApprovalPresentation.BATCH_ACTION
    assert len(response.pending_approval.operations) == 2
    left_out = json.loads(tool_results(model)[-1])["not_on_card"]
    assert left_out == [{"shopping_item_id": "dish-1", "reason": NEEDS_LOCATION}]


@pytest.mark.parametrize(
    "item, reason",
    [
        ({"shopping_item_id": "gone", "quantity": 1}, "待購清單上找不到"),
        ({"shopping_item_id": "tissue-1", "quantity": 3}, "比待購清單上多"),
    ],
)
def test_nothing_fits_means_no_card(item, reason):
    container, reader, approvals = setup()
    model = ScriptedModel(purchase_reply(item, call_id="call-1"), AIMessage(content="好"))

    response = ask(agent(model, reader, approvals))

    assert response.pending_approval is None
    assert reason in tool_results(model)[-1]
    assert container.of_type("pending_approval") == []


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"shopping_item_id": "tissue-1", "quantity": 0}],
        [{"shopping_item_id": "tissue-1", "quantity": True}],
        [{"shopping_item_id": "tissue-1", "quantity": 1.5}],
        [{"shopping_item_id": "tissue-1", "quantity": 1}] * 2,
    ],
)
def test_unusable_model_arguments_make_no_card(items):
    container, reader, approvals = setup()
    model = ScriptedModel(
        _call("complete_purchase", items=items), AIMessage(content="請再說一次買了什麼。")
    )

    response = ask(agent(model, reader, approvals))

    assert response.pending_approval is None
    assert container.of_type("pending_approval") == []


def test_one_card_per_message():
    container, reader, approvals = setup()
    item = {"shopping_item_id": "tissue-1", "quantity": 1}
    model = ScriptedModel(
        purchase_reply(item, call_id="call-1"),
        purchase_reply(item, call_id="call-2"),
        AIMessage(content="請確認"),
    )

    ask(agent(model, reader, approvals))

    assert len(container.of_type("pending_approval")) == 1
    assert json.loads(tool_results(model)[-1]) == {"error": "card_already_made"}


def test_card_belongs_to_the_requests_household():
    container, reader, approvals = setup()
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": 1}, call_id="call-1"),
        AIMessage(content="找不到"),
    )

    response = ask(agent(model, reader, approvals), household_id="another-household")

    assert response.pending_approval is None  # another household's list is not read
    assert container.of_type("pending_approval") == []


def test_card_tool_is_offered_only_with_approval_cards():
    _, reader, approvals = setup()
    with_cards = ScriptedModel()
    without = ScriptedModel()

    agent(with_cards, reader, approvals)
    ReadAgent(without, QueryTools(CosmosInventoryService(reader), None), load_system_prompt())

    names = {tool["function"]["name"] for tool in with_cards.bound_tools}
    assert "complete_purchase" in names
    assert "complete_purchase" not in {tool["function"]["name"] for tool in without.bound_tools}


def test_without_cosmos_there_are_no_cards():
    assert _create_cosmos_composition({})[2:] == (None, None, None)
    assert _create_approval_service(None, None, None) is None


def test_reads_writes_and_cards_share_one_cosmos_client(monkeypatch):
    """B1's review on PR #96: one SDK client for reads, writes and C1's raw reads."""
    container = FakeCosmosContainer()
    read_repository = CosmosReadRepository(container)
    write_repository = CosmosWriteRepository(container, query_client=container)
    document_reader = container
    clients = []

    def one_client(**settings):
        clients.append(settings)
        return read_repository, write_repository, document_reader, lambda: None

    def no_second_client(**settings):
        raise AssertionError("cards must not open a second Cosmos client")

    monkeypatch.setattr(dependencies, "create_azure_cosmos_repositories", one_client)
    monkeypatch.setattr(
        "app.services.cosmos_sdk.create_azure_cosmos_read_repository", no_second_client
    )
    monkeypatch.setattr(
        "app.services.cosmos_sdk.create_azure_cosmos_repositories", no_second_client
    )

    inventory, shopping, reads, writes, documents = _create_cosmos_composition(
        {
            "NESTOCK_COSMOS_ENDPOINT": "https://example.documents.azure.com",
            "NESTOCK_COSMOS_DATABASE": "nestock",
            "NESTOCK_COSMOS_CONTAINER": "household-data",
        }
    )
    approvals = _create_approval_service(reads, writes, documents)

    assert len(clients) == 1
    assert reads is read_repository and writes is write_repository
    assert documents is document_reader
    assert inventory._repository is read_repository
    assert shopping._repository is read_repository
    assert approvals._storage is write_repository
    # Issue #60: C1 plans the batch from raw documents; A1 resolves the stored card.
    assert isinstance(approvals._writes, ShoppingWriteBuilder)
    assert approvals._writes._reader is document_reader
    assert isinstance(approvals._writes._resolver, CardInputResolver)
    assert approvals._writes._resolver._reader is read_repository


def resume_app(approvals):
    app = create_app()
    app.dependency_overrides[get_approval_service] = lambda: approvals
    app.dependency_overrides[get_read_agent] = lambda: None
    return app


def made_card(reader, approvals, quantity=2):
    model = ScriptedModel(
        purchase_reply({"shopping_item_id": "tissue-1", "quantity": quantity}, call_id="call-1"),
        AIMessage(content="請確認"),
    )
    return ask(agent(model, reader, approvals)).pending_approval


def test_confirming_the_card_writes_it_and_says_what_was_recorded():
    container, reader, approvals = setup()
    card = made_card(reader, approvals)
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": True}

    with TestClient(resume_app(approvals)) as client:
        first = client.post("/agent/resume", json=body)
        again = client.post("/agent/resume", json=body)  # a double tap writes once

    assert first.status_code == 200
    assert first.json()["message"] == "已記錄：衛生紙 +2 包（現在 3 包）"
    assert first.json()["results"][0]["kind"] == "action_result"
    assert again.json()["message"] == first.json()["message"]
    assert container.get("inventory_item:tissue")["quantity"] == 3
    assert container.get("shopping_item:tissue-1")["status"] == "completed"
    assert len(container.of_type("transaction")) == 1


@pytest.mark.skipif(
    ShoppingItem.model_fields["quantity"].is_required(),
    reason="Shopping entries without quantity or unit need B1's PR #64 schema",
)
@pytest.mark.parametrize(
    "missing, bought",
    [
        ({"quantity": None}, 3),  # no listed quantity: nothing to be "more than"
        ({"unit": None}, 2),  # no listed unit: the inventory unit goes on the card
        ({"quantity": None, "unit": None}, 2),  # name only (Issue #52)
    ],
)
def test_entries_without_quantity_or_unit_get_a_card_and_confirm(missing, bought):
    container, reader, approvals = setup(inventory(), shopping(**missing))

    card = made_card(reader, approvals, quantity=bought)

    assert card is not None
    line = card.operations[0]
    assert line.description == f"衛生紙 +{bought} 包（從待購清單完成購買）"
    assert line.values["purchased_unit"] == "包"
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": True}
    with TestClient(resume_app(approvals)) as client:
        response = client.post("/agent/resume", json=body)
    assert response.status_code == 200
    assert response.json()["message"] == f"已記錄：衛生紙 +{bought} 包（現在 {1 + bought} 包）"
    assert container.get("inventory_item:tissue")["quantity"] == 1 + bought
    assert container.get("shopping_item:tissue-1")["status"] == "completed"
    assert len(container.of_type("transaction")) == 1


def test_cancelling_the_card_writes_nothing():
    container, reader, approvals = setup()
    card = made_card(reader, approvals)

    with TestClient(resume_app(approvals)) as client:
        response = client.post(
            "/agent/resume",
            json={"household_id": HOME, "approval_id": card.approval_id, "approved": False},
        )

    assert response.json()["message"] == "已取消，沒有記錄任何東西。"
    assert container.get("inventory_item:tissue")["quantity"] == 1
    assert container.of_type("transaction") == []


def test_unknown_card_and_wrong_selection_are_refused():
    container, reader, approvals = setup()
    card = made_card(reader, approvals)

    with TestClient(resume_app(approvals)) as client:
        unknown = client.post(
            "/agent/resume",
            json={"household_id": HOME, "approval_id": "nope", "approved": True},
        )
        other_household = client.post(
            "/agent/resume",
            json={"household_id": "other", "approval_id": card.approval_id, "approved": True},
        )
        wrong_selection = client.post(
            "/agent/resume",
            json={
                "household_id": HOME,
                "approval_id": card.approval_id,
                "approved": True,
                "selected_option_id": "o-1",
            },
        )

    assert unknown.status_code == 404
    assert other_household.status_code == 404
    assert wrong_selection.status_code == 409
    assert container.get("inventory_item:tissue")["quantity"] == 1


def test_changed_record_after_the_card_asks_for_a_new_one():
    container, reader, approvals = setup()
    card = made_card(reader, approvals)
    container.put(inventory(quantity=5))

    with TestClient(resume_app(approvals)) as client:
        response = client.post(
            "/agent/resume",
            json={"household_id": HOME, "approval_id": card.approval_id, "approved": True},
        )

    assert "剛剛被改過" in response.json()["message"]
    assert container.get("inventory_item:tissue")["quantity"] == 5


@pytest.mark.parametrize(
    "outcome, words",
    [
        (ResumeOutcome.EXPIRED, "過期"),
        (ResumeOutcome.NOT_WRITTEN, "再按一次確認"),
    ],
)
def test_reply_never_says_done_unless_written(outcome, words):
    response = resume_response(ResumeResult(outcome))

    assert words in response.message
    assert "已記錄" not in response.message


def test_inconsistent_write_state_is_reported_not_hidden():
    with pytest.raises(RuntimeError):
        resume_response(ResumeResult(ResumeOutcome.INCONSISTENT))


# ----- a list entry the household does not have yet (Issue #90, contract section 10) --


def paste(unit="條", quantity=1):
    """牙膏 on the list, not linked to an inventory item: the household has none."""
    return shopping("paste-1", "牙膏", None, quantity, unit)


def new_item_card(reader, approvals, **item):
    """The model asks for 牙膏 1 條 to the bathroom cabinet; `None` leaves a word out."""
    said = {"shopping_item_id": "paste-1", "quantity": 1, "location": "浴室櫃子", **item}
    model = ScriptedModel(
        purchase_reply(
            {key: value for key, value in said.items() if value is not None}, call_id="call-1"
        ),
        AIMessage(content="請確認下方的卡片，按確認後才會記錄。"),
    )
    return ask(agent(model, reader, approvals), "牙膏買回來了，1 條，放浴室櫃子"), model


def test_a_list_entry_the_household_lacks_gets_a_new_item_card_and_nothing_changes():
    container, reader, approvals = setup(inventory(), paste())

    response, model = new_item_card(reader, approvals)

    card = response.pending_approval
    assert card.presentation is ApprovalPresentation.SINGLE_ACTION
    (line,) = card.operations
    assert line.action == CREATE_ITEM
    assert line.description == "會新增一個庫存品項：牙膏 1 條，放在浴室櫃子（從待購清單完成購買）"
    assert {k: v for k, v in line.values.items() if k != "transaction_id"} == {
        "name": "牙膏",
        "quantity": 1,
        "unit": "條",
        "location": "浴室櫃子",
        "shopping_item_id": "paste-1",
    }
    assert "item_id" not in line.values  # made only when the user confirms
    stored = container.get(f"pending_approval:{card.approval_id}")
    assert [t["record_id"] for t in stored["operations"][0]["targets"]] == ["shopping_item:paste-1"]
    assert len(container.of_type("inventory_item")) == 1
    assert container.get("shopping_item:paste-1")["status"] == "pending"
    assert container.of_type("transaction") == []
    assert json.loads(tool_results(model)[-1])["card"]["lines"] == [line.description]


def test_confirming_a_new_item_card_creates_the_item_once():
    container, reader, approvals = setup(inventory(), paste())
    card = new_item_card(reader, approvals)[0].pending_approval
    body = {"household_id": HOME, "approval_id": card.approval_id, "approved": True}

    with TestClient(resume_app(approvals)) as client:
        first = client.post("/agent/resume", json=body)
        again = client.post("/agent/resume", json=body)  # a double tap writes once

    assert first.status_code == 200
    assert first.json()["message"] == "已記錄：牙膏 +1 條（現在 1 條）"
    assert again.json()["message"] == first.json()["message"]
    item_id = resolve_inventory_item_id(card.operations[0].operation_id)
    created = container.get(f"inventory_item:{item_id}")
    assert (created["name"], created["quantity"], created["unit"], created["location"]) == (
        "牙膏",
        1,
        "條",
        "浴室櫃子",
    )
    assert len(container.of_type("inventory_item")) == 2
    assert container.get("shopping_item:paste-1")["status"] == "completed"
    (transaction,) = container.of_type("transaction")
    assert (transaction["item_id"], transaction["quantity_delta"]) == (item_id, 1)


def test_cancelling_a_new_item_card_creates_nothing():
    container, reader, approvals = setup(inventory(), paste())
    card = new_item_card(reader, approvals)[0].pending_approval

    with TestClient(resume_app(approvals)) as client:
        response = client.post(
            "/agent/resume",
            json={"household_id": HOME, "approval_id": card.approval_id, "approved": False},
        )

    assert response.json()["message"] == "已取消，沒有記錄任何東西。"
    assert len(container.of_type("inventory_item")) == 1
    assert container.get("shopping_item:paste-1")["status"] == "pending"
    assert container.of_type("transaction") == []


@pytest.mark.skipif(
    ShoppingItem.model_fields["unit"].is_required(),
    reason="Shopping entries without a unit need B1's PR #64 schema",
)
def test_an_entry_without_a_unit_uses_the_unit_the_user_said():
    container, reader, approvals = setup(inventory(), paste(unit=None))

    card = new_item_card(reader, approvals, unit="條")[0].pending_approval

    assert card.operations[0].values["unit"] == "條"


def test_a_new_item_sits_on_one_card_with_ordinary_purchases_and_both_are_written():
    container, reader, approvals = setup(inventory(), shopping(), paste())
    model = ScriptedModel(
        purchase_reply(
            {"shopping_item_id": "tissue-1", "quantity": 2},
            {"shopping_item_id": "paste-1", "quantity": 1, "location": "浴室櫃子"},
            call_id="call-1",
        ),
        AIMessage(content="請確認"),
    )
    card = ask(agent(model, reader, approvals)).pending_approval
    assert card.presentation is ApprovalPresentation.BATCH_ACTION
    assert [line.action for line in card.operations] == [COMPLETE_PURCHASE, CREATE_ITEM]

    body = {
        "household_id": HOME,
        "approval_id": card.approval_id,
        "approved": True,
        "approved_operation_ids": [line.operation_id for line in card.operations],
    }
    with TestClient(resume_app(approvals)) as client:
        response = client.post("/agent/resume", json=body)

    assert response.json()["message"] == (
        "已記錄：衛生紙 +2 包（現在 3 包）、牙膏 +1 條（現在 1 條）"
    )
    assert container.get("inventory_item:tissue")["quantity"] == 3
    assert len(container.of_type("inventory_item")) == 2
    assert len(container.of_type("transaction")) == 2


@pytest.mark.parametrize(
    "records, item, reason",
    [
        ([paste()], {"location": None}, NEEDS_LOCATION),
        ([paste()], {"unit": "支"}, OTHER_UNIT),
        ([paste()], {"quantity": 2}, MORE_THAN_LISTED),
        ([inventory("paste", "牙膏", 0, "條"), paste()], {}, ALREADY_HAVE.format(names="牙膏")),
        (
            [inventory("paste", "高露潔", 1, "條", aliases=["牙膏"]), paste()],
            {},
            ALREADY_HAVE.format(names="高露潔"),
        ),
    ],
)
def test_no_new_item_line_without_location_matching_unit_or_when_the_household_has_it(
    records, item, reason
):
    container, reader, approvals = setup(inventory(), *records)

    response, model = new_item_card(reader, approvals, **item)

    assert response.pending_approval is None
    result = json.loads(tool_results(model)[-1])
    assert result == {
        "card": None,
        "not_on_card": [{"shopping_item_id": "paste-1", "reason": reason}],
    }
    assert container.of_type("pending_approval") == []


@pytest.mark.skipif(
    ShoppingItem.model_fields["unit"].is_required(),
    reason="Shopping entries without a unit need B1's PR #64 schema",
)
def test_no_new_item_line_without_any_unit():
    container, reader, approvals = setup(inventory(), paste(unit=None))

    response, model = new_item_card(reader, approvals)

    assert response.pending_approval is None
    assert json.loads(tool_results(model)[-1])["not_on_card"][0]["reason"] == NEEDS_UNIT


@pytest.mark.parametrize("words", [{"location": ""}, {"location": 3}, {"unit": " "}])
def test_unusable_unit_or_location_make_no_card(words):
    container, reader, approvals = setup(inventory(), paste())

    response, _ = new_item_card(reader, approvals, **words)

    assert response.pending_approval is None
    assert container.of_type("pending_approval") == []
