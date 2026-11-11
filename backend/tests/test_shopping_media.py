"""C1 attachment retention joins the existing approval/domain batch; synthetic data only."""

from dataclasses import replace
from datetime import timedelta
from importlib import import_module
from importlib.util import find_spec

import pytest
from test_shopping_workflow import HOME, request, run, setup

from app.services.approvals import BatchTooLarge, ResumeOutcome, TargetChanged, TargetVersion
from app.services.cosmos_write import ReplaceDocument
from app.services.shopping.media import (
    B1MediaRetentionPlanner,
    MediaAttachment,
)
from app.services.shopping.preview import ShoppingDecisionRequired, ShoppingPreviewError


class SyntheticRetentionPlanner:
    """Stand-in until #97 lands; joint test below exercises the actual B1 adapter."""

    def __init__(self, container):
        self.container = container

    async def prepare_attachment(self, *, household_id, attachment):
        document = self.container.get(f"input:{attachment.input_id}", household_id)
        if document is None or document["_etag"] != attachment.mapping_etag:
            raise TargetChanged(attachment.input_id)
        if document["status"] != "completed":
            raise ShoppingDecisionRequired("Media is not completed.")
        kept = {key: value for key, value in document.items() if key not in ("_etag", "ttl")}
        return ReplaceDocument(kept, attachment.mapping_etag)


def attach(h, *, selected=None, provider=True):
    mapping_id = "synthetic"
    reference = "opaque-test-media"
    etag = h.container.put(
        {
            "id": f"input:{mapping_id}",
            "record_type": "user_input",
            "household_id": HOME,
            "input_id": mapping_id,
            "image_ref": reference,
            "status": "completed",
            "ttl": 86400,
        }
    )
    if selected is None:
        selected = h.card.operation_ids
    bindings, operations = [], []
    for operation in h.card.operations:
        if operation.operation_id in selected:
            bindings.append(MediaAttachment(operation.operation_id, mapping_id, reference, etag))
            operation = operation.model_copy(
                update={
                    "values": {**operation.values, "image_ref": reference},
                    "targets": [
                        *operation.targets,
                        TargetVersion(record_id=f"input:{mapping_id}", etag=etag),
                    ],
                }
            )
        operations.append(operation)
    h.card = h.card.model_copy(update={"operations": operations})
    original = h.resolver.resolve

    async def resolve(record):
        changes = await original(record)
        return replace(
            changes,
            media_attachments=tuple(
                row for row in bindings if row.operation_id in record.approved_operation_ids
            ),
        )

    h.resolver.resolve = resolve
    if provider:
        h.builder._media_retention = SyntheticRetentionPlanner(h.container)
    return bindings


@pytest.mark.parametrize("kind", ["existing", "new"])
def test_attachment_and_retention_are_in_one_batch_with_purchase_and_retry(kind):
    h = setup((kind,), save=False)
    bindings = attach(h)
    run(h.service.save(h.card))
    assert h.container.get("input:synthetic")["ttl"] == 86400
    assert all(doc.get("image_ref") is None for doc in h.container.of_type("inventory_item"))
    h.container.lose_next_domain_batch_response = True
    assert run(h.service.resume(request(h))).written
    mapping = h.container.get("input:synthetic")
    assert "ttl" not in mapping
    result = run(h.service.resume(request(h)))
    assert result.outcome == ResumeOutcome.ALREADY_COMPLETED
    item_id = result.record.outcome[bindings[0].operation_id]["item_id"]
    assert h.container.get(f"inventory_item:{item_id}")["image_ref"] == "opaque-test-media"
    assert h.container.get("input:synthetic") == mapping
    assert sum(op.document["id"] == "input:synthetic" for op in h.container.batches[-1]) == 1


def test_shared_mapping_is_updated_once_and_counted_in_hundred_action_limit():
    h = setup(("existing",) * 22 + ("new",) * 2, save=False)
    attach(h)  # Existing 100-action batch + one shared mapping update = 101.
    with pytest.raises(BatchTooLarge):
        run(h.service.save(h.card))
    assert not h.container.of_type("pending_approval")
    assert h.container.get("input:synthetic")["ttl"] == 86400


def test_cancel_and_partial_selection_leave_unattached_media_on_orphan_ttl():
    h = setup(("existing", "new"), save=False)
    attach(h, selected=[h.card.operation_ids[1]])
    run(h.service.save(h.card))
    assert run(h.service.resume(request(h, selected=[h.card.operation_ids[0]]))).written
    assert h.container.get("input:synthetic")["ttl"] == 86400
    h = setup(("new",), save=False)
    attach(h)
    run(h.service.save(h.card))
    assert run(h.service.resume(request(h, approve=False))).outcome == ResumeOutcome.CANCELLED
    assert h.container.get("input:synthetic")["ttl"] == 86400


def test_mapping_etag_race_rolls_back_all_domain_and_retention_writes():
    h = setup(save=False)
    attach(h)
    run(h.service.save(h.card))

    def race():
        h.container.put(dict(h.container.get("input:synthetic")))

    h.container.before_next_domain_batch = race
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.NEEDS_NEW_CARD
    assert h.container.get("input:synthetic")["ttl"] == 86400
    assert h.container.get("inventory_item:milk")["quantity"] == 3
    assert h.container.get("inventory_item:milk")["image_ref"] is None
    assert not h.container.of_type("operation_receipt")
    assert not h.container.of_type("transaction")


@pytest.mark.parametrize("invalid", ["not_completed", "other_household", "unshown", "no_port"])
def test_unready_foreign_unshown_or_unconfigured_media_cannot_be_attached(invalid):
    h = setup(save=False)
    attach(h, provider=invalid != "no_port")
    if invalid == "not_completed":
        h.container.get("input:synthetic")["status"] = "processing"
    elif invalid == "other_household":
        document = h.container.documents.pop((HOME, "input:synthetic"))
        document["household_id"] = "other"
        h.container.put(document)
    elif invalid == "unshown":
        operation = h.card.operations[0]
        h.card = h.card.model_copy(
            update={
                "operations": [
                    operation.model_copy(
                        update={
                            "values": {
                                key: value
                                for key, value in operation.values.items()
                                if key != "image_ref"
                            }
                        }
                    )
                ]
            }
        )
    with pytest.raises((ShoppingPreviewError, TargetChanged)):
        run(h.service.save(h.card))
    assert not h.container.of_type("pending_approval")
    assert h.container.get("inventory_item:milk")["quantity"] == 3


@pytest.mark.skipif(find_spec("app.services.media_mapping") is None, reason="Awaiting B1 PR #97")
def test_actual_b1_port_returns_completed_same_household_retention_operation():
    from test_media_mapping import FakeMediaMappingClient

    from app.models.domain import MediaInputMapping
    from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
    from app.services.cosmos_write import CreateDocument

    media_module = import_module("app.services.media_mapping")
    CosmosMediaMappingStorage = media_module.CosmosMediaMappingStorage
    MediaMappingNotReady = media_module.MediaMappingNotReady
    h = setup(save=False)
    mapping = MediaInputMapping(
        input_id="port-test",
        household_id=HOME,
        image_ref="opaque-port-test",
        content_type="image/jpeg",
        expected_file_size_bytes=10,
        file_size_bytes=10,
        sha256_hash="a" * 64,
        artifact_ref="opaque-test-artifact",
        status="completed",
        upload_authorization_expires_at=h.clock.now + timedelta(minutes=5),
        created_at=h.clock.now,
        updated_at=h.clock.now,
        ttl_seconds=86400,
    )
    client = FakeMediaMappingClient()
    run(
        client.execute_batch(
            partition_key=HOME,
            operations=(CreateDocument(CosmosDocumentMapper.to_document(mapping)),),
        )
    )
    stored = client.documents[(HOME, "input:port-test")]
    etag = stored["_etag"]
    storage = CosmosMediaMappingStorage(
        read_repository=CosmosReadRepository(client), write_client=client
    )
    planner = B1MediaRetentionPlanner(storage)
    attachment = MediaAttachment("test-op", "port-test", "opaque-port-test", etag)
    operation = run(planner.prepare_attachment(household_id=HOME, attachment=attachment))
    assert operation.document["id"] == "input:port-test"
    assert operation.if_match_etag == etag
    assert "ttl" not in operation.document
    assert stored["ttl"] == 86400  # Planning only.
    for household, changed in [
        ("other", attachment),
        (HOME, replace(attachment, mapping_etag="stale")),
    ]:
        with pytest.raises(TargetChanged):
            run(planner.prepare_attachment(household_id=household, attachment=changed))
    stored["status"] = "processing"
    stored["artifact_ref"] = None
    with pytest.raises(MediaMappingNotReady):
        run(planner.prepare_attachment(household_id=HOME, attachment=attachment))


@pytest.mark.skipif(find_spec("app.services.media_mapping") is None, reason="Awaiting B1 PR #97")
def test_actual_b1_retention_helper_joins_real_c1_a1_b1_purchase_batch():
    from test_media_mapping import FakeMediaMappingClient

    from app.models.domain import MediaInputMapping
    from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository

    media_module = import_module("app.services.media_mapping")
    h = setup(("new",), save=False)
    attach(h, provider=False)
    # Complete synthetic fixture metadata; preserve the initial card-bound ETag.
    document = h.container.get("input:synthetic")
    mapping = MediaInputMapping(
        input_id="synthetic",
        household_id=HOME,
        image_ref="opaque-test-media",
        content_type="image/jpeg",
        expected_file_size_bytes=10,
        file_size_bytes=10,
        sha256_hash="a" * 64,
        artifact_ref="opaque-artifact",
        status="completed",
        created_at=h.clock.now,
        updated_at=h.clock.now,
        upload_authorization_expires_at=h.clock.now + timedelta(minutes=5),
        ttl_seconds=86400,
    )
    document.update(CosmosDocumentMapper.to_document(mapping))
    query = FakeMediaMappingClient()
    query.documents = h.container.documents
    storage = media_module.CosmosMediaMappingStorage(
        read_repository=CosmosReadRepository(query), write_client=h.container
    )
    h.builder._media_retention = B1MediaRetentionPlanner(storage)
    run(h.service.save(h.card))
    assert document["ttl"] == 86400
    result = run(h.service.resume(request(h)))
    assert result.written
    outcome = result.record.outcome[h.card.operation_ids[0]]
    assert h.container.get(f"inventory_item:{outcome['item_id']}")["image_ref"] == mapping.image_ref
    assert "ttl" not in h.container.get("input:synthetic")
    batch = h.container.batches[-1]
    assert {op.document["record_type"] for op in batch} == {
        "inventory_item",
        "shopping_item",
        "transaction",
        "change_event",
        "operation_receipt",
        "user_input",
        "pending_approval",
    }
    assert run(h.service.resume(request(h))).outcome == ResumeOutcome.ALREADY_COMPLETED


def test_attachment_cannot_silently_replace_a_different_existing_image():
    h = setup(save=False)
    operation_id = h.card.operation_ids[0]
    source = h.resolver.inputs[operation_id]
    h.resolver.inputs[operation_id] = replace(
        source, inventory=source.inventory.model_copy(update={"image_ref": "previous-image"})
    )
    h.container.get("inventory_item:milk")["image_ref"] = "previous-image"
    attach(h)
    with pytest.raises(ShoppingDecisionRequired, match="detach/remaining-reference"):
        run(h.service.save(h.card))
    assert h.container.get("inventory_item:milk")["image_ref"] == "previous-image"
    assert h.container.get("input:synthetic")["ttl"] == 86400
    assert not h.container.of_type("pending_approval")


@pytest.mark.parametrize(
    "wrong_field,wrong_value",
    [("household_id", "other"), ("ttl", 3600), ("if_match_etag", "wrong-etag")],
)
def test_retention_port_cannot_emit_foreign_or_expiring_attachment_update(wrong_field, wrong_value):
    h = setup(save=False)
    attach(h)
    original = h.builder._media_retention.prepare_attachment

    async def incorrect_output(**kwargs):
        operation = await original(**kwargs)
        return ReplaceDocument(
            {
                **operation.document,
                **({wrong_field: wrong_value} if wrong_field != "if_match_etag" else {}),
            },
            wrong_value if wrong_field == "if_match_etag" else operation.if_match_etag,
        )

    h.builder._media_retention.prepare_attachment = incorrect_output
    with pytest.raises(ShoppingPreviewError, match="Retention update"):
        run(h.service.save(h.card))
    assert h.container.get("input:synthetic")["ttl"] == 86400
    assert not h.container.of_type("pending_approval")
