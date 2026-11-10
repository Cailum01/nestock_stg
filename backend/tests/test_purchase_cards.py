"""A1 v8 flow C end to end: the Agent makes a purchase card, `/agent/resume` writes it.

A scripted model stands in for Azure OpenAI. B1's read and write repositories run on
a faked Cosmos container that gives every write a new `_etag`. The approval service
and the purchase adapter are the real ones.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.cards import PurchaseCards, resume_response
from app.agent.read_agent import ReadAgent, get_read_agent, load_system_prompt
from app.core.dependencies import _create_approval_service, get_approval_service
from app.main import create_app
from app.models.agent import AgentRequest, ApprovalPresentation
from app.models.domain import InventoryItem, ShoppingItem, ShoppingStatus
from app.services.approvals import ApprovalService, ResumeOutcome, ResumeResult
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
from app.services.purchase_writes import PurchaseWriteBuilder
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


def inventory(item_id="tissue", name="衛生紙", quantity=1.0, unit="包"):
    return InventoryItem(
        item_id=item_id,
        household_id=HOME,
        name=name,
        quantity=quantity,
        unit=unit,
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
        PurchaseWriteBuilder(reader),
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


def test_several_items_go_on_one_card_and_unfit_ones_are_left_off_with_a_reason():
    container, reader, approvals = setup(
        inventory(),
        inventory("milk", "牛奶", 0, "瓶"),
        shopping(),
        shopping("milk-1", "牛奶", "milk", 1, "瓶"),
        shopping("dish-1", "洗碗精", None, 1, "瓶"),  # not linked to inventory (Issue #90)
    )
    model = ScriptedModel(
        purchase_reply(
            {"shopping_item_id": "tissue-1", "quantity": 2},
            {"shopping_item_id": "milk-1", "quantity": 1},
            {"shopping_item_id": "dish-1", "quantity": 1},
            call_id="call-1",
        ),
        AIMessage(content="衛生紙和牛奶請確認；洗碗精現在還不能記錄。"),
    )

    response = ask(agent(model, reader, approvals))

    assert response.pending_approval.presentation is ApprovalPresentation.BATCH_ACTION
    assert len(response.pending_approval.operations) == 2
    left_out = json.loads(tool_results(model)[-1])["not_on_card"]
    assert left_out == [
        {
            "shopping_item_id": "dish-1",
            "reason": "這樣東西還沒有對應到庫存裡的品項，現在還不能從對話記錄購買。",
        }
    ]


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
    assert _create_approval_service(None) is None


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
