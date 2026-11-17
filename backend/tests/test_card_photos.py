"""A1: a photo sent with the message is kept with the record it adds to (Issue #176).

Photo new-item cards and purchase cards with exactly one line hold the photo's
`image_ref`, `input_id` and mapping ETag, fixed when the card is made. Confirming
hands exactly these on: B1's photo writer for a new item, C1's purchase batch (with
B1's retention planner and work-item port, as composed in PR #197) for a purchase.
The scripted model stands in for Azure OpenAI; Cosmos is faked; the approval service,
A1's cards and resolver, C1's builder and B1's storage are the real ones.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage
from test_purchase_cards import (
    HOME,
    T0,
    FakeCosmosContainer,
    ScriptedModel,
    _call,
    inventory,
    shopping,
    tool_results,
)
from test_read_agent import _detected, _StubVision

from app.agent.cards import (
    CARD_CHANGED,
    PHOTO_CARD_MADE,
    PHOTO_ITEM_HAS_ONE,
    PHOTO_KEPT,
    PHOTO_NOT_ON_MATCH,
    PHOTO_NOT_READY,
    PHOTO_ONE_LINE_ONLY,
    PhotoCards,
    PurchaseCards,
)
from app.agent.read_agent import ReadAgent, load_system_prompt
from app.models.agent import AgentRequest, CardChange, ResumeRequest
from app.models.domain import MediaInputMapping, MediaProcessingStatus
from app.services.approvals import ApprovalService, ResumeOutcome, StoredOperation
from app.services.card_writes import CardWriteRouter
from app.services.cosmos_read import (
    CosmosInventoryService,
    CosmosReadRepository,
    CosmosShoppingService,
)
from app.services.cosmos_write import CosmosWriteRepository
from app.services.media_mapping import CosmosMediaMappingStorage
from app.services.media_retention_work import CosmosMediaRetentionWorkItemStorage
from app.services.photo_item_writes import PhotoItemWriteBuilder
from app.services.purchase_inputs import CardInputResolver, PhotoInputResolver, stored_photo
from app.services.shopping.media import B1MediaRetentionPlanner, MediaAttachment
from app.services.shopping.media_handoff import B1MediaRetentionWorkItemAdapter
from app.services.shopping.workflow import ShoppingWriteBuilder
from app.tools.queries import QueryTools

PHOTO = "img-receipt-001"
INPUT = "input-receipt-001"
PHOTO_KEYS = {"image_ref", "input_id", "mapping_etag"}


def run(coro):
    return asyncio.run(coro)


class FakeMediaCosmosContainer(FakeCosmosContainer):
    """The purchase tests' container, also finding a media mapping by its image_ref."""

    async def query_items(self, *, query, parameters, partition_key):
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        if values.get("@record_type") == "user_input":
            return [
                dict(document)
                for (household_id, _), document in self.documents.items()
                if household_id == partition_key == values["@household_id"]
                and document.get("record_type") == "user_input"
                and document.get("image_ref") == values["@image_ref"]
            ]
        return await super().query_items(
            query=query, parameters=parameters, partition_key=partition_key
        )


def _mapping(container, *, status=MediaProcessingStatus.COMPLETED, household_id=HOME):
    """A synthetic mapping for PHOTO, walked to `status` the way A2's upload does."""
    storage = CosmosMediaMappingStorage(
        read_repository=CosmosReadRepository(container), write_client=container
    )
    snapshot = run(
        storage.create_mapping(
            MediaInputMapping(
                input_id=INPUT,
                household_id=household_id,
                image_ref=PHOTO,
                content_type="image/jpeg",
                expected_file_size_bytes=100,
                status=MediaProcessingStatus.ALLOCATED,
                upload_authorization_expires_at=T0 + timedelta(minutes=5),
                created_at=T0,
                updated_at=T0,
            ),
            orphan_ttl_seconds=86_400,
        )
    )
    steps = [
        (MediaProcessingStatus.RECEIVED, {"file_size_bytes": 100, "sha256_hash": "a" * 64}),
        (MediaProcessingStatus.PROCESSING, {}),
        (MediaProcessingStatus.COMPLETED, {"artifact_ref": "opaque-artifact"}),
    ]
    for step, extra in steps:
        if snapshot.record.status is status:
            break
        snapshot = run(
            storage.transition(
                household_id=household_id,
                input_id=INPUT,
                expected_etag=snapshot.etag,
                status=step,
                updated_at=T0,
                **extra,
            )
        )
    return storage, snapshot


class _Media:
    """A2's media service as the Agent sees it: the photo exists; its mapping is B1's."""

    def __init__(self, storage):
        self.storage = storage

    async def verify_media_exists(self, household_id, image_ref):
        return True

    async def get_media_snapshot(self, household_id, image_ref):
        return await self.storage.find_by_image_ref(household_id=household_id, image_ref=image_ref)


def setup(*records, status=MediaProcessingStatus.COMPLETED, mapping_household=HOME):
    """Cosmos with `records` and the photo's mapping, composed as after PR #197."""
    container = FakeMediaCosmosContainer()
    for record in records or (inventory(), shopping()):
        container.put(record)
    reader = CosmosReadRepository(container)
    storage, _ = _mapping(container, status=status, household_id=mapping_household)
    work_items = B1MediaRetentionWorkItemAdapter(
        CosmosMediaRetentionWorkItemStorage(write_client=container, query_client=container)
    )
    builder = CardWriteRouter(
        purchases=ShoppingWriteBuilder(
            container,
            CardInputResolver(reader),
            media_retention=B1MediaRetentionPlanner(storage),
            media_work_items=work_items,
        ),
        photo_items=PhotoItemWriteBuilder(PhotoInputResolver(), media_mapping_storage=storage),
        household_items=reader,
    )
    approvals = ApprovalService(
        CosmosWriteRepository(container, query_client=container), builder, clock=lambda: T0
    )
    return container, reader, approvals, storage


def _agent(model, reader, approvals, storage, *detected):
    tools = QueryTools(CosmosInventoryService(reader), CosmosShoppingService(reader))
    return ReadAgent(
        model,
        tools,
        load_system_prompt(),
        vision=_StubVision(*detected),
        media=_Media(storage),
        purchases=PurchaseCards(reader, approvals),
        photo_cards=PhotoCards(approvals),
    )


def _ask(agent, message):
    return run(agent.respond(AgentRequest(household_id=HOME, message=message, image_ref=PHOTO)))


def _bought(*items):
    """The model looks at the photo, then records the purchase."""
    return ScriptedModel(
        _call("analyze_photo", "call-1"),
        _call("complete_purchase", "call-2", items=list(items)),
        AIMessage(content="請確認。"),
    )


def _confirm(approvals, card, **selection):
    request = ResumeRequest(
        household_id=HOME, approval_id=card.approval_id, approved=True, **selection
    )
    return run(approvals.resume(request))


def _mapping_document(container):
    return container.get(f"input:{INPUT}")


# Photo new-item cards


def _photo_card(container, reader, approvals, storage, *detected):
    model = ScriptedModel(_call("analyze_photo"), AIMessage(content="請確認。"))
    agent = _agent(model, reader, approvals, storage, *detected)
    return _ask(agent, "幫我把照片裡的東西加進庫存").pending_approval, model


def test_a_photo_new_item_card_keeps_the_photo_and_says_so():
    container, reader, approvals, storage = setup()
    etag = _mapping_document(container)["_etag"]

    card, model = _photo_card(container, reader, approvals, storage, _detected("滑鼠", unit="個"))

    [line] = card.operations
    assert {key: line.values[key] for key in PHOTO_KEYS} == {
        "image_ref": PHOTO,
        "input_id": INPUT,
        "mapping_etag": etag,
    }
    assert line.description == "會新增一個庫存品項：滑鼠 1 個（照片辨識）"
    seen = json.loads(tool_results(model)[-1])
    assert seen["new_item_card"]["note"] == PHOTO_CARD_MADE
    assert seen["new_item_card"]["photo"] == PHOTO_KEPT


def test_items_from_one_photo_share_it_and_confirming_keeps_it_once():
    container, reader, approvals, storage = setup(inventory())
    card, _ = _photo_card(
        container,
        reader,
        approvals,
        storage,
        _detected("滑鼠", unit="個"),
        _detected("鍵盤", unit="個"),
    )
    assert [line.values["image_ref"] for line in card.operations] == [PHOTO, PHOTO]

    every = [line.operation_id for line in card.operations]
    first = _confirm(approvals, card, approved_operation_ids=every)
    again = _confirm(approvals, card, approved_operation_ids=every)  # a double tap writes once

    assert first.outcome is ResumeOutcome.COMPLETED
    assert again.outcome is ResumeOutcome.ALREADY_COMPLETED
    created = [d for d in container.of_type("inventory_item") if d["name"] in {"滑鼠", "鍵盤"}]
    assert sorted(d["image_ref"] for d in created) == [PHOTO, PHOTO]
    assert "ttl" not in _mapping_document(container)  # kept, no longer an orphan
    assert len(container.of_type("transaction")) == 2


@pytest.mark.parametrize(
    "status, mapping_household",
    [
        (MediaProcessingStatus.PROCESSING, HOME),  # not completed yet
        (MediaProcessingStatus.COMPLETED, "other-household"),  # not this household's
    ],
)
def test_a_photo_that_is_not_ready_is_not_kept_and_the_model_says_so(status, mapping_household):
    container, reader, approvals, storage = setup(
        status=status, mapping_household=mapping_household
    )
    card, model = _photo_card(container, reader, approvals, storage, _detected("滑鼠", unit="個"))

    [line] = card.operations
    assert not PHOTO_KEYS & line.values.keys()
    assert json.loads(tool_results(model)[-1])["new_item_card"]["photo"] == PHOTO_NOT_READY


def test_changing_a_photo_new_item_card_keeps_its_photo():
    container, reader, approvals, storage = setup()
    card, _ = _photo_card(container, reader, approvals, storage, _detected("滑鼠", unit="個"))
    [line] = card.operations
    model = ScriptedModel()
    agent = _agent(model, reader, approvals, storage)

    response = run(
        agent.respond(
            AgentRequest(
                household_id=HOME,
                message="送出修改",
                replaces_approval_id=card.approval_id,
                card_changes=[CardChange(operation_id=line.operation_id, location="書房")],
            )
        )
    )

    assert response.message == CARD_CHANGED
    [changed] = response.pending_approval.operations
    assert changed.values["location"] == "書房"
    assert {k: changed.values[k] for k in PHOTO_KEYS} == {k: line.values[k] for k in PHOTO_KEYS}


# Purchase cards


def test_a_one_line_purchase_card_keeps_the_photo_and_confirming_keeps_it_once():
    container, reader, approvals, storage = setup()
    etag = _mapping_document(container)["_etag"]
    model = _bought({"shopping_item_id": "tissue-1", "quantity": 2})

    card = _ask(_agent(model, reader, approvals, storage), "我買了衛生紙 2 包").pending_approval

    [line] = card.operations
    assert {k: line.values[k] for k in PHOTO_KEYS} == {
        "image_ref": PHOTO,
        "input_id": INPUT,
        "mapping_etag": etag,
    }
    assert json.loads(tool_results(model)[-1])["photo"] == PHOTO_KEPT

    first = _confirm(approvals, card)
    again = _confirm(approvals, card)

    assert first.outcome is ResumeOutcome.COMPLETED
    assert again.outcome is ResumeOutcome.ALREADY_COMPLETED
    assert container.get("inventory_item:tissue")["image_ref"] == PHOTO
    assert container.get("inventory_item:tissue")["quantity"] == 3
    assert "ttl" not in _mapping_document(container)
    [work_item] = container.of_type("media_retention_work_item")
    assert (work_item["image_ref"], work_item["status"]) == (PHOTO, "pending")
    assert len(container.of_type("transaction")) == 1


def test_changing_a_purchase_card_keeps_its_photo():
    container, reader, approvals, storage = setup()
    model = _bought({"shopping_item_id": "tissue-1", "quantity": 2})
    agent = _agent(model, reader, approvals, storage)
    card = _ask(agent, "我買了衛生紙 2 包").pending_approval
    [line] = card.operations

    response = run(
        agent.respond(
            AgentRequest(
                household_id=HOME,
                message="送出修改",
                replaces_approval_id=card.approval_id,
                card_changes=[CardChange(operation_id=line.operation_id, quantity=3)],
            )
        )
    )

    assert response.message == CARD_CHANGED
    [changed] = response.pending_approval.operations
    assert changed.values["purchased_quantity"] == 3
    assert {k: changed.values[k] for k in PHOTO_KEYS} == {k: line.values[k] for k in PHOTO_KEYS}
    assert _confirm(approvals, response.pending_approval).outcome is ResumeOutcome.COMPLETED
    assert container.get("inventory_item:tissue")["image_ref"] == PHOTO


def test_a_mapping_that_cannot_be_read_keeps_no_photo():
    class _Unreadable(_Media):
        async def get_media_snapshot(self, household_id, image_ref):
            raise ConnectionError("Cosmos is unreachable")

    container, reader, approvals, storage = setup()
    model = _bought({"shopping_item_id": "tissue-1", "quantity": 2})
    agent = _agent(model, reader, approvals, storage)
    agent._media = _Unreadable(storage)

    card = _ask(agent, "我買了衛生紙 2 包").pending_approval

    assert not PHOTO_KEYS & card.operations[0].values.keys()
    assert json.loads(tool_results(model)[-1])["photo"] == PHOTO_NOT_READY


def test_a_new_item_bought_from_the_list_keeps_the_photo():
    container, reader, approvals, storage = setup(
        shopping("paste-1", name="牙膏", item_id=None, unit="條")
    )
    model = _bought({"shopping_item_id": "paste-1", "quantity": 1, "location": "浴室"})

    card = _ask(_agent(model, reader, approvals, storage), "我買了牙膏").pending_approval

    assert card.operations[0].values["image_ref"] == PHOTO
    assert _confirm(approvals, card).outcome is ResumeOutcome.COMPLETED
    [created] = container.of_type("inventory_item")
    assert (created["name"], created["image_ref"]) == ("牙膏", PHOTO)


def test_an_item_with_its_own_photo_keeps_it_and_the_model_says_so():
    has_photo = inventory().model_copy(update={"image_ref": "img-older"})
    container, reader, approvals, storage = setup(has_photo, shopping())
    model = _bought({"shopping_item_id": "tissue-1", "quantity": 2})

    card = _ask(_agent(model, reader, approvals, storage), "我買了衛生紙 2 包").pending_approval

    assert not PHOTO_KEYS & card.operations[0].values.keys()
    assert json.loads(tool_results(model)[-1])["photo"] == PHOTO_ITEM_HAS_ONE.format(name="衛生紙")
    assert _confirm(approvals, card).outcome is ResumeOutcome.COMPLETED
    assert container.get("inventory_item:tissue")["image_ref"] == "img-older"


def test_a_card_with_several_lines_keeps_no_photo():
    container, reader, approvals, storage = setup(
        inventory(),
        shopping(),
        inventory("soap", "肥皂", unit="塊"),
        shopping("soap-1", name="肥皂", item_id="soap", unit="塊"),
    )
    model = _bought(
        {"shopping_item_id": "tissue-1", "quantity": 2},
        {"shopping_item_id": "soap-1", "quantity": 1},
    )

    card = _ask(_agent(model, reader, approvals, storage), "我買了衛生紙和肥皂").pending_approval

    assert len(card.operations) == 2
    assert all(not PHOTO_KEYS & line.values.keys() for line in card.operations)
    assert json.loads(tool_results(model)[-1])["photo"] == PHOTO_ONE_LINE_ONLY


def test_an_item_match_card_keeps_no_photo():
    container, reader, approvals, storage = setup(
        inventory("soap", "洗潔精", unit="瓶"),
        shopping("dish-1", name="洗碗精", item_id=None, unit="瓶"),
    )
    model = _bought({"shopping_item_id": "dish-1", "quantity": 1, "candidate_item_id": "soap"})

    card = _ask(_agent(model, reader, approvals, storage), "我買了洗碗精").pending_approval

    assert card.presentation.value == "item_match"
    assert not PHOTO_KEYS & card.operations[0].values.keys()
    assert json.loads(tool_results(model)[-1])["photo"] == PHOTO_NOT_ON_MATCH


def test_a_purchase_without_a_photo_is_unchanged():
    container, reader, approvals, storage = setup()
    model = ScriptedModel(
        _call(
            "complete_purchase", "call-1", items=[{"shopping_item_id": "tissue-1", "quantity": 2}]
        ),
        AIMessage(content="請確認。"),
    )
    agent = _agent(model, reader, approvals, storage)

    card = run(agent.respond(AgentRequest(household_id=HOME, message="我買了衛生紙 2 包")))

    assert not PHOTO_KEYS & card.pending_approval.operations[0].values.keys()
    assert "photo" not in json.loads(tool_results(model)[-1])
    assert _confirm(approvals, card.pending_approval).outcome is ResumeOutcome.COMPLETED
    assert container.get("inventory_item:tissue").get("image_ref") is None
    assert container.of_type("media_retention_work_item") == []


# CardInputResolver: hands the card's photo to C1 as it is


def _stored(values, targets):
    return StoredOperation(
        operation_id="op-1",
        action="complete_purchase",
        description="衛生紙 +2 包",
        values={
            "name": "衛生紙",
            "purchased_quantity": 2,
            "purchased_unit": "包",
            "shopping_item_id": "tissue-1",
            "item_id": "tissue",
            "transaction_id": "tx-1",
            **values,
        },
        targets=targets,
    )


def test_resolving_twice_hands_c1_the_same_attachment_from_the_card():
    container, reader, approvals, storage = setup()
    model = _bought({"shopping_item_id": "tissue-1", "quantity": 2})
    card = _ask(_agent(model, reader, approvals, storage), "我買了衛生紙 2 包").pending_approval
    record = run(approvals.read_card(household_id=HOME, approval_id=card.approval_id))
    record = record.model_copy(update={"approved_operation_ids": record.operation_ids})
    resolver = CardInputResolver(reader)

    first = run(resolver.resolve(record))
    # The mapping changes after the card: the resolver still hands over the card's values.
    _mapping_document(container)["_etag"] = '"etag-later"'
    second = run(resolver.resolve(record))

    [line] = record.operations
    expected = MediaAttachment(
        operation_id=line.operation_id,
        input_id=INPUT,
        image_ref=PHOTO,
        mapping_etag=line.values["mapping_etag"],
    )
    assert first.media_attachments == second.media_attachments == (expected,)


@pytest.mark.parametrize(
    "values",
    [
        {"image_ref": PHOTO},
        {"image_ref": PHOTO, "input_id": INPUT},
        {"input_id": INPUT, "mapping_etag": '"e"'},
        {"image_ref": "", "input_id": INPUT, "mapping_etag": '"e"'},
    ],
)
def test_part_of_a_photo_is_refused(values):
    with pytest.raises(ValueError):
        stored_photo(_stored(values, []).values)


def test_a_photo_line_must_carry_its_mapping_at_the_cards_etag():
    container, reader, approvals, storage = setup()
    model = _bought({"shopping_item_id": "tissue-1", "quantity": 2})
    card = _ask(_agent(model, reader, approvals, storage), "我買了衛生紙 2 包").pending_approval
    record = run(approvals.read_card(household_id=HOME, approval_id=card.approval_id))
    [line] = record.operations
    targets = [t for t in line.targets if not t.record_id.startswith("input:")]
    wrong_etag = [*targets, line.targets[-1].model_copy(update={"etag": '"other"'})]

    for changed in (targets, wrong_etag):
        bad = record.model_copy(
            update={
                "operations": [line.model_copy(update={"targets": changed})],
                "approved_operation_ids": record.operation_ids,
            }
        )
        with pytest.raises(ValueError):
            run(CardInputResolver(reader).resolve(bad))
