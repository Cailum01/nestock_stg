import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

import pytest
from pydantic import ValidationError

from app.models.domain import (
    InventoryItem,
    ShoppingItem,
    ShoppingStatus,
    Transaction,
    TransactionReason,
)
from app.services.demo_seed import (
    DEMO_HOUSEHOLD_ID,
    DemoSeedError,
    DemoSeedManifest,
    load_demo_seed,
    load_manifest,
)

SEED_TIME = datetime(2026, 11, 13, tzinfo=UTC)


class FakeSeedContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, object]] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.queried_partitions: list[str] = []
        self.batch_calls: list[dict[str, object]] = []
        self.fail_batch_after: int | None = None
        self.fail_create_after: int | None = None

    def query_items(
        self,
        *,
        query: str,
        parameters: list[Mapping[str, object]],
        partition_key: str,
        response_hook,
    ) -> list[dict[str, object]]:
        assert query == "SELECT * FROM c WHERE c.household_id = @household_id"
        assert parameters == [{"name": "@household_id", "value": partition_key}]
        self.queried_partitions.append(partition_key)
        response_hook({"x-ms-request-charge": "1"}, {})
        return [
            deepcopy(document)
            for document in self.documents.values()
            if document.get("household_id") == partition_key
        ]

    def create_item(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        document = dict(body)
        document_id = str(document["id"])
        if document_id in self.documents:
            raise AssertionError(f"duplicate create for {document_id}")
        if self.fail_create_after is not None and len(self.created) >= self.fail_create_after:
            raise RuntimeError("injected item create failure")
        self.documents[document_id] = document
        self.created.append(document_id)
        return document

    def delete_item(self, *, item: str, partition_key: str) -> None:
        document = self.documents[item]
        assert document["household_id"] == partition_key
        self.deleted.append(item)
        del self.documents[item]

    def execute_item_batch(self, *, batch_operations, partition_key, response_hook):
        self.batch_calls.append(
            {"batch_operations": batch_operations, "partition_key": partition_key}
        )
        staged = deepcopy(self.documents)
        created: list[str] = []
        deleted: list[str] = []
        for index, (operation, arguments) in enumerate(batch_operations, start=1):
            if operation == "delete":
                item = str(arguments[0])
                document = staged[item]
                assert document["household_id"] == partition_key
                del staged[item]
                deleted.append(item)
            elif operation == "create":
                document = dict(arguments[0])
                document_id = str(document["id"])
                if document_id in staged:
                    raise AssertionError(f"duplicate create for {document_id}")
                staged[document_id] = document
                created.append(document_id)
            else:
                raise AssertionError(f"unsupported batch operation {operation!r}")

            if self.fail_batch_after == index:
                raise RuntimeError("injected transactional batch failure")

        self.documents = staged
        self.created.extend(created)
        self.deleted.extend(deleted)
        response_hook({"x-ms-request-charge": "1"}, [])
        return []


def make_manifest() -> DemoSeedManifest:
    inventory = InventoryItem(
        item_id="seed-detergent",
        household_id=DEMO_HOUSEHOLD_ID,
        name="洗衣精",
        quantity=1,
        unit="瓶",
        created_at=SEED_TIME,
        updated_at=SEED_TIME,
    )
    shopping = ShoppingItem(
        shopping_item_id="seed-shopping-detergent",
        household_id=DEMO_HOUSEHOLD_ID,
        name="洗衣精",
        quantity=2,
        unit="瓶",
        status=ShoppingStatus.PENDING,
        item_id=inventory.item_id,
        created_at=SEED_TIME,
        updated_at=SEED_TIME,
    )
    transaction = Transaction(
        transaction_id="seed-transaction-detergent-1",
        household_id=DEMO_HOUSEHOLD_ID,
        item_id=inventory.item_id,
        operation_id="seed-operation-detergent-1",
        quantity_delta=-1,
        reason=TransactionReason.CONSUMPTION,
        created_at=SEED_TIME,
    )
    return DemoSeedManifest(
        household_id=DEMO_HOUSEHOLD_ID,
        inventory=[inventory],
        shopping=[shopping],
        transactions=[transaction],
    )


def test_seed_dry_run_does_not_write_and_is_demo_partition_scoped():
    container = FakeSeedContainer()

    result = load_demo_seed(container, make_manifest())

    assert result.action == "load"
    assert result.applied is False
    assert result.records_planned == 3
    assert result.records_created == 0
    assert result.documents_deleted == 0
    assert container.created == []
    assert container.deleted == []
    assert container.queried_partitions == [DEMO_HOUSEHOLD_ID]


def test_manifest_loads_all_record_types_from_one_json_file(tmp_path):
    manifest_path = tmp_path / "demo-seed.json"
    manifest_path.write_text(
        json.dumps(make_manifest().model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    manifest = load_manifest(manifest_path)

    assert len(manifest.inventory) == 1
    assert len(manifest.shopping) == 1
    assert len(manifest.transactions) == 1


def test_seed_load_creates_typed_records_and_rerun_is_idempotent():
    container = FakeSeedContainer()
    manifest = make_manifest()

    result = load_demo_seed(container, manifest, apply=True)

    assert result.records_created == 3
    assert len(container.documents) == 3
    assert all(
        document["household_id"] == DEMO_HOUSEHOLD_ID for document in container.documents.values()
    )
    assert container.documents["inventory_item:seed-detergent"]["quantity"] == 1
    assert container.documents["shopping_item:seed-shopping-detergent"]["status"] == "pending"
    assert container.documents["transaction:seed-transaction-detergent-1"]["quantity_delta"] == -1

    for document in container.documents.values():
        document["_etag"] = '"seed-etag"'
    created_before_retry = list(container.created)

    retry = load_demo_seed(container, manifest, apply=True)

    assert retry.action == "already_loaded"
    assert retry.records_created == 0
    assert container.created == created_before_retry


def test_interrupted_seed_refuses_to_mutate_partial_partition_on_retry():
    container = FakeSeedContainer()
    manifest = make_manifest()
    container.fail_create_after = 1

    with pytest.raises(RuntimeError, match="injected item create failure"):
        load_demo_seed(container, manifest, apply=True)

    partial_state = deepcopy(container.documents)
    created_before_retry = list(container.created)
    assert len(partial_state) == 1

    container.fail_create_after = None
    with pytest.raises(DemoSeedError, match="does not exactly match"):
        load_demo_seed(container, manifest, apply=True)

    assert container.documents == partial_state
    assert container.created == created_before_retry
    assert container.deleted == []


def test_seed_refuses_to_overwrite_unrecognized_or_partial_household_state():
    container = FakeSeedContainer()
    container.documents["inventory_item:legacy"] = {
        "id": "inventory_item:legacy",
        "record_type": "inventory_item",
        "household_id": DEMO_HOUSEHOLD_ID,
    }

    with pytest.raises(DemoSeedError, match="does not exactly match"):
        load_demo_seed(container, make_manifest(), apply=True)

    assert container.created == []
    assert container.deleted == []


def test_reset_dry_run_reports_scope_without_deleting():
    container = FakeSeedContainer()
    container.documents["inventory_item:old-demo"] = {
        "id": "inventory_item:old-demo",
        "household_id": DEMO_HOUSEHOLD_ID,
    }
    container.documents["inventory_item:other"] = {
        "id": "inventory_item:other",
        "household_id": "other-household",
    }

    result = load_demo_seed(container, make_manifest(), reset=True)

    assert result.action == "reset"
    assert result.applied is False
    assert result.records_planned == 3
    assert result.documents_deleted == 0
    assert container.deleted == []
    assert container.created == []
    assert container.batch_calls == []


def test_reset_only_deletes_demo_household_then_reloads_the_manifest():
    container = FakeSeedContainer()
    operation_receipt_id = f"operation:{'a' * 64}"
    container.documents["inventory_item:old-demo"] = {
        "id": "inventory_item:old-demo",
        "household_id": DEMO_HOUSEHOLD_ID,
    }
    container.documents["pending_approval:old-demo"] = {
        "id": "pending_approval:old-demo",
        "household_id": DEMO_HOUSEHOLD_ID,
        "record_type": "pending_approval",
    }
    container.documents[operation_receipt_id] = {
        "id": operation_receipt_id,
        "household_id": DEMO_HOUSEHOLD_ID,
        "record_type": "operation_receipt",
    }
    container.documents["inventory_item:other"] = {
        "id": "inventory_item:other",
        "household_id": "other-household",
    }

    with pytest.raises(DemoSeedError, match="explicit demo-household confirmation"):
        load_demo_seed(container, make_manifest(), apply=True, reset=True)

    result = load_demo_seed(
        container,
        make_manifest(),
        apply=True,
        reset=True,
        confirm_household_wipe=DEMO_HOUSEHOLD_ID,
    )

    assert result.documents_deleted == 3
    assert result.records_created == 3
    assert container.deleted == [
        "inventory_item:old-demo",
        "pending_approval:old-demo",
        operation_receipt_id,
    ]
    assert len(container.batch_calls) == 1
    assert container.batch_calls[0]["partition_key"] == DEMO_HOUSEHOLD_ID
    assert [operation for operation, _ in container.batch_calls[0]["batch_operations"]] == [
        "delete",
        "delete",
        "delete",
        "create",
        "create",
        "create",
    ]
    assert container.documents["inventory_item:other"]["household_id"] == "other-household"
    assert (
        len(
            [
                document
                for document in container.documents.values()
                if document["household_id"] == DEMO_HOUSEHOLD_ID
            ]
        )
        == 3
    )


def test_reset_batch_failure_preserves_previous_partition_state():
    container = FakeSeedContainer()
    container.documents = {
        "inventory_item:old-demo": {
            "id": "inventory_item:old-demo",
            "household_id": DEMO_HOUSEHOLD_ID,
        },
        "pending_approval:old-demo": {
            "id": "pending_approval:old-demo",
            "household_id": DEMO_HOUSEHOLD_ID,
        },
        "inventory_item:other": {
            "id": "inventory_item:other",
            "household_id": "other-household",
        },
    }
    original_documents = deepcopy(container.documents)
    container.fail_batch_after = 2

    with pytest.raises(RuntimeError, match="injected transactional batch failure"):
        load_demo_seed(
            container,
            make_manifest(),
            apply=True,
            reset=True,
            confirm_household_wipe=DEMO_HOUSEHOLD_ID,
        )

    assert container.documents == original_documents
    assert len(container.batch_calls) == 1
    assert container.created == []
    assert container.deleted == []


def test_reset_fails_before_batch_when_operation_limit_would_be_exceeded():
    container = FakeSeedContainer()
    container.documents = {
        f"inventory_item:old-demo-{index}": {
            "id": f"inventory_item:old-demo-{index}",
            "household_id": DEMO_HOUSEHOLD_ID,
        }
        for index in range(100)
    }
    original_documents = deepcopy(container.documents)

    with pytest.raises(DemoSeedError, match="No writes were submitted"):
        load_demo_seed(
            container,
            make_manifest(),
            apply=True,
            reset=True,
            confirm_household_wipe=DEMO_HOUSEHOLD_ID,
        )

    assert container.documents == original_documents
    assert container.batch_calls == []
    assert container.created == []
    assert container.deleted == []


def test_manifest_rejects_non_demo_household_and_dangling_transaction_item():
    manifest = make_manifest().model_dump()
    manifest["household_id"] = "other-household"

    with pytest.raises(ValidationError, match="household_id"):
        DemoSeedManifest.model_validate(manifest)

    manifest = make_manifest().model_dump()
    manifest["transactions"][0]["item_id"] = "missing-item"
    with pytest.raises(ValidationError, match="outside the seed inventory"):
        DemoSeedManifest.model_validate(manifest)


def test_manifest_loads_unlinked_shopping_item():
    manifest_data = make_manifest().model_dump(mode="json")
    manifest_data["shopping"].append(
        {
            "shopping_item_id": "seed-shopping-toothpaste",
            "household_id": DEMO_HOUSEHOLD_ID,
            "name": "牙膏",
            "quantity": 1,
            "unit": "條",
            "status": ShoppingStatus.PENDING.value,
            "item_id": None,
            "created_at": SEED_TIME.isoformat(),
            "updated_at": SEED_TIME.isoformat(),
        }
    )
    manifest = DemoSeedManifest.model_validate(manifest_data)
    container = FakeSeedContainer()

    result = load_demo_seed(container, manifest, apply=True)

    toothpaste = next(
        document for document in container.documents.values() if document.get("name") == "牙膏"
    )
    assert result.records_created == 4
    assert toothpaste.get("item_id") is None


def test_official_demo_manifest_loads_and_retries_as_noop():
    repository_root = Path(__file__).resolve().parents[2]
    manifest = load_manifest(repository_root / "data" / "demo_household_manifest.json")
    container = FakeSeedContainer()

    assert (len(manifest.inventory), len(manifest.shopping), len(manifest.transactions)) == (
        7,
        3,
        21,
    )
    result = load_demo_seed(container, manifest, apply=True)

    assert result.records_created == 31
    assert len(container.documents) == 31
    toothpaste = next(
        document for document in container.documents.values() if document.get("name") == "牙膏"
    )
    assert toothpaste.get("item_id") is None

    retry = load_demo_seed(container, manifest, apply=True)

    assert retry.action == "already_loaded"
    assert retry.records_created == 0
    assert len(container.created) == 31
