"""Opt-in synthetic C1 acceptance on Cosmos; never discovered by pytest.

Run only with user permission. Requires the isolated #97 dependency snapshot (or
its merged code). Leaves the c1-test-* partition for B1 review; never touches demo.
The resolver is a test adapter using #95's existing-purchase card values, not the
production A1 Agent resolver. No routes, credentials or Azure settings are changed.
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from importlib import import_module

from azure.cosmos import CosmosClient
from azure.identity import AzureCliCredential

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import InventoryItem, ShoppingItem
from app.services.approvals import ApprovalService, NewOperation, ResumeOutcome, TargetVersion
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_sdk import (
    AzureCosmosDocumentQueryClient,
    AzureCosmosTransactionalBatchClient,
)
from app.services.cosmos_write import CosmosWriteRepository, CreateDocument
from app.services.shopping.media import B1MediaRetentionPlanner, MediaAttachment
from app.services.shopping.planning import NewInventoryTarget, PurchaseInput
from app.services.shopping.workflow import ResolvedShoppingChanges, ShoppingWriteBuilder


class AcceptanceResolver:
    """Test-only stored-card decoding, with original target versions checked by C1."""

    def __init__(self, reader):
        self.reader = reader

    async def resolve(self, record):
        purchases, attachments = [], []
        for operation in record.operations:
            if operation.operation_id not in record.approved_operation_ids:
                continue
            values = operation.values
            if operation.action != "complete_purchase":
                raise ValueError("Unsupported synthetic test action")
            quantity = values["purchased_quantity"]
            if type(quantity) is not int or quantity <= 0:
                raise ValueError("Stored actual quantity must be a positive integer")
            shopping_doc = await self.reader.read_item(
                item_id=f"shopping_item:{values['shopping_item_id']}",
                partition_key=record.household_id,
            )
            shopping = CosmosDocumentMapper.from_document(
                ShoppingItem, shopping_doc, household_id=record.household_id
            )
            versions = {target.record_id: target.etag for target in operation.targets}
            inventory, inventory_etag, new = None, None, None
            if "item_id" in values:
                inventory_doc = await self.reader.read_item(
                    item_id=f"inventory_item:{values['item_id']}", partition_key=record.household_id
                )
                inventory = CosmosDocumentMapper.from_document(
                    InventoryItem, inventory_doc, household_id=record.household_id
                )
                inventory_etag = versions[inventory_doc["id"]]
            else:
                # Only the test's explicit new-item card reaches this branch.
                new = NewInventoryTarget(
                    import_module("app.services.inventory_item_identity").resolve_inventory_item_id(
                        operation.operation_id
                    ),
                    values["name"],
                    values["purchased_unit"],
                    values["location"],
                )
            purchases.append(
                PurchaseInput(
                    operation_id=operation.operation_id,
                    transaction_id=values["transaction_id"],
                    shopping=shopping,
                    shopping_etag=versions[shopping_doc["id"]],
                    inventory=inventory,
                    inventory_etag=inventory_etag,
                    purchased_quantity=quantity,
                    purchased_unit=values["purchased_unit"],
                    new_inventory=new,
                )
            )
            if values.get("image_ref"):
                # Test fixture input ID; not a proposed public wire field.
                attachments.append(
                    MediaAttachment(
                        operation.operation_id,
                        "synthetic-media",
                        values["image_ref"],
                        versions["input:synthetic-media"],
                    )
                )
        return ResolvedShoppingChanges(
            purchases=tuple(purchases), media_attachments=tuple(attachments)
        )


async def verify(args, container):
    household = args.household
    MediaInputMapping = import_module("app.models.domain").MediaInputMapping
    CosmosMediaMappingStorage = import_module(
        "app.services.media_mapping"
    ).CosmosMediaMappingStorage
    resolve_inventory_item_id = import_module(
        "app.services.inventory_item_identity"
    ).resolve_inventory_item_id
    reader = AzureCosmosDocumentQueryClient(container)
    writer = AzureCosmosTransactionalBatchClient(container)
    repo = CosmosWriteRepository(writer, query_client=reader)
    storage = CosmosMediaMappingStorage(
        read_repository=CosmosReadRepository(reader), write_client=writer
    )
    builder = ShoppingWriteBuilder(
        reader, AcceptanceResolver(reader), media_retention=B1MediaRetentionPlanner(storage)
    )
    service = ApprovalService(repo, builder)
    now = datetime.now(UTC)
    checks = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    async def read(identity):
        return await reader.read_item(item_id=identity, partition_key=household)

    async def records(kind):
        return await reader.query_items(
            query="SELECT * FROM c WHERE c.household_id = @home AND c.record_type = @kind",
            parameters=[{"name": "@home", "value": household}, {"name": "@kind", "value": kind}],
            partition_key=household,
        )

    # Refuse reuse of a nonempty household, including any previously run test.
    existing = await reader.query_items(
        query="SELECT TOP 1 * FROM c WHERE c.household_id = @home",
        parameters=[{"name": "@home", "value": household}],
        partition_key=household,
    )
    if existing:
        raise ValueError("Synthetic household already contains records; use a new ID")
    if container.read().get("defaultTtl") != -1:
        raise ValueError("Container TTL not configured; no settings were changed")
    stock = InventoryItem(
        item_id="tissue",
        household_id=household,
        name="衛生紙",
        quantity=1,
        unit="包",
        created_at=now,
        updated_at=now,
    )
    shopping = ShoppingItem(
        shopping_item_id="tissue",
        household_id=household,
        name="衛生紙",
        quantity=2,
        unit="包",
        item_id="tissue",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    new_shopping = ShoppingItem(
        shopping_item_id="soap",
        household_id=household,
        name="洗碗精",
        quantity=1,
        unit="瓶",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    image_ref = f"opaque-synthetic-{household}"
    media = MediaInputMapping(
        input_id="synthetic-media",
        household_id=household,
        image_ref=image_ref,
        content_type="image/jpeg",
        expected_file_size_bytes=10,
        file_size_bytes=10,
        sha256_hash="a" * 64,
        artifact_ref=f"opaque-synthetic-artifact-{household}",
        status="completed",
        upload_authorization_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
        ttl_seconds=86400,
    )
    await writer.execute_batch(
        partition_key=household,
        operations=tuple(
            CreateDocument(CosmosDocumentMapper.to_document(record))
            for record in (stock, shopping, new_shopping, media)
        ),
    )
    inventory_doc, shopping_doc = (
        await read("inventory_item:tissue"),
        await read("shopping_item:tissue"),
    )
    check("synthetic_seed", inventory_doc["quantity"] == 1 and shopping_doc["quantity"] == 2)
    operation = NewOperation(
        action="complete_purchase",
        description="合成測試：衛生紙 +2 包",
        values={
            "name": "衛生紙",
            "purchased_quantity": 2,
            "purchased_unit": "包",
            "shopping_item_id": "tissue",
            "item_id": "tissue",
            "transaction_id": "tx-tissue",
        },
        targets=[
            TargetVersion(record_id=doc["id"], etag=doc["_etag"])
            for doc in (shopping_doc, inventory_doc)
        ],
    )
    card = await service.save(
        service.prepare(
            household_id=household,
            presentation=ApprovalPresentation.SINGLE_ACTION,
            operations=[operation],
        )
    )
    check(
        "pending_card_stored",
        (await read(f"pending_approval:{card.approval_id}"))["status"] == "pending",
    )
    check(
        "stock_unchanged_before_confirmation",
        (await read("inventory_item:tissue"))["quantity"] == 1,
    )
    request = ResumeRequest(household_id=household, approval_id=card.approval_id, approved=True)
    result = await service.resume(request)
    check("confirmation_completed", result.outcome == ResumeOutcome.COMPLETED)
    check("stock_now_three", (await read("inventory_item:tissue"))["quantity"] == 3)
    check("shopping_completed", (await read("shopping_item:tissue"))["status"] == "completed")
    tx = await records("transaction")
    check("one_actual_purchase_transaction", len(tx) == 1 and tx[0]["quantity_delta"] == 2)
    check("one_change_event", len(await records("change_event")) == 1)
    check(
        "stored_outcome_for_a1_response",
        result.record.outcome[card.operation_ids[0]]["inventory_quantity"] == 3
        and result.record.outcome[card.operation_ids[0]]["unit"] == "包",
    )
    check(
        "repeat_already_completed",
        (await service.resume(request)).outcome == ResumeOutcome.ALREADY_COMPLETED,
    )
    check(
        "no_repeat_purchase_effects",
        (await read("inventory_item:tissue"))["quantity"] == 3
        and len(await records("transaction")) == 1,
    )
    # New Inventory + real B1 stable ID + real B1 mapping-retention helper in the same batch.
    shopping_doc, media_doc = await read("shopping_item:soap"), await read("input:synthetic-media")
    new_card = await service.save(
        service.prepare(
            household_id=household,
            presentation=ApprovalPresentation.SINGLE_ACTION,
            operations=[
                NewOperation(
                    action="complete_purchase",
                    description="合成測試：會新增洗碗精 1 瓶，附加合成媒體",
                    values={
                        "name": "洗碗精",
                        "purchased_quantity": 1,
                        "purchased_unit": "瓶",
                        "shopping_item_id": "soap",
                        "transaction_id": "tx-soap",
                        "location": "廚房",
                        "image_ref": image_ref,
                    },
                    targets=[
                        TargetVersion(record_id=doc["id"], etag=doc["_etag"])
                        for doc in (shopping_doc, media_doc)
                    ],
                )
            ],
        )
    )
    new_id = resolve_inventory_item_id(new_card.operation_ids[0])
    check(
        "new_item_not_created_before_confirmation", await read(f"inventory_item:{new_id}") is None
    )
    check(
        "orphan_ttl_unchanged_before_confirmation",
        (await read("input:synthetic-media"))["ttl"] == 86400,
    )
    new_request = ResumeRequest(
        household_id=household, approval_id=new_card.approval_id, approved=True
    )
    check("new_purchase_completed", (await service.resume(new_request)).written)
    created = await read(f"inventory_item:{new_id}")
    check(
        "new_item_with_attached_media",
        created["quantity"] == 1 and created["image_ref"] == image_ref,
    )
    check(
        "retention_updated_with_business_attachment",
        "ttl" not in await read("input:synthetic-media"),
    )
    check(
        "new_purchase_repeat_already_completed",
        (await service.resume(new_request)).outcome == ResumeOutcome.ALREADY_COMPLETED,
    )
    check(
        "new_purchase_no_duplicate_effects",
        len(await records("inventory_item")) == 2
        and len(await records("transaction")) == 2
        and len(await records("change_event")) == 2,
    )
    return {
        "household_id": household,
        "checks_passed": len(checks),
        "checks": checks,
        "scope": "C1 builder + test A1 resolver + real B1 SDK/repository; not Agent/App E2E",
        "retained_for_b1_review": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="Explicit synthetic data-plane write opt-in"
    )
    parser.add_argument("--household", required=True)
    parser.add_argument(
        "--endpoint", default="https://nestock-cosmos-dev-20261101.documents.azure.com"
    )
    args = parser.parse_args()
    if (
        not args.execute
        or not args.household.startswith("c1-test-")
        or args.household == "demo-household"
    ):
        parser.error("Requires --execute and a new c1-test-* household")
    try:
        import_module("app.services.media_mapping")
        import_module("app.services.inventory_item_identity")
    except ImportError:
        parser.error("B1 PR #97 dependencies are required; use the isolated dependency snapshot")
    # The installed SDK tries account-specific audiences by default. Azure CLI
    # may receive AADSTS65002 for that audience; use the supported service scope
    # only for this opt-in test process, without editing .env or CLI settings.
    previous_scope = os.environ.get("AZURE_COSMOS_AAD_SCOPE_OVERRIDE")
    os.environ["AZURE_COSMOS_AAD_SCOPE_OVERRIDE"] = "https://cosmos.azure.com/.default"
    credential = AzureCliCredential()
    client = None
    try:
        client = CosmosClient(args.endpoint, credential=credential)
        container = client.get_database_client("nestock").get_container_client("household-data")
        print(json.dumps(asyncio.run(verify(args, container)), ensure_ascii=False))
    finally:
        if client is not None:
            client.close()
        credential.close()
        if previous_scope is None:
            os.environ.pop("AZURE_COSMOS_AAD_SCOPE_OVERRIDE", None)
        else:
            os.environ["AZURE_COSMOS_AAD_SCOPE_OVERRIDE"] = previous_scope


if __name__ == "__main__":
    main()
