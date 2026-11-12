import json
from copy import deepcopy
from datetime import UTC, datetime
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
        self.documents[document_id] = document
        self.created.append(document_id)
        return document

    def delete_item(self, *, item: str, partition_key: str) -> None:
        document = self.documents[item]
        assert document["household_id"] == partition_key
        self.deleted.append(item)
        del self.documents[item]


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


def test_reset_only_deletes_demo_household_then_reloads_the_manifest():
    container = FakeSeedContainer()
    container.documents["inventory_item:old-demo"] = {
        "id": "inventory_item:old-demo",
        "household_id": DEMO_HOUSEHOLD_ID,
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

    assert result.documents_deleted == 1
    assert result.records_created == 3
    assert container.deleted == ["inventory_item:old-demo"]
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


def test_manifest_rejects_non_demo_household_and_dangling_transaction_item():
    manifest = make_manifest().model_dump()
    manifest["household_id"] = "other-household"

    with pytest.raises(ValidationError, match="household_id"):
        DemoSeedManifest.model_validate(manifest)

    manifest = make_manifest().model_dump()
    manifest["transactions"][0]["item_id"] = "missing-item"
    with pytest.raises(ValidationError, match="outside the seed inventory"):
        DemoSeedManifest.model_validate(manifest)
