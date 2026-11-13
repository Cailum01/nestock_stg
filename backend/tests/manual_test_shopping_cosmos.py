"""Opt-in synthetic B1 persistence and C1 purchase acceptance on Cosmos.

Run only with user permission. Requires the isolated #97 dependency snapshot (or
its merged code). Leaves the c1-test-* partition for B1 review; never touches demo.
Existing-item purchases use A1's production CardInputResolver. A test adapter
covers new-item purchases and removals not supported by that resolver. This is not
Agent/App E2E. No routes, credentials or Azure settings are changed.
"""

import argparse
import asyncio
import io
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from importlib import import_module

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosClientTimeoutError
from azure.identity import AzureCliCredential

from app.models.agent import ApprovalPresentation, ResumeRequest
from app.models.domain import InventoryItem, ShoppingItem
from app.services.approvals import (
    ApprovalService,
    NewOperation,
    ResumeOutcome,
    TargetVersion,
)
from app.services.cosmos_read import CosmosDocumentMapper, CosmosReadRepository
from app.services.cosmos_sdk import (
    AzureCosmosDocumentQueryClient,
    AzureCosmosTransactionalBatchClient,
)
from app.services.cosmos_write import (
    BatchOperationConflict,
    CosmosWriteRepository,
    CreateDocument,
    OperationIdConflict,
    OperationReceiptInconsistency,
    ReplaceDocument,
    WriteStatus,
)
from app.services.purchase_inputs import CardInputResolver
from app.services.shopping.media import B1MediaRetentionPlanner, MediaAttachment
from app.services.shopping.planning import NewInventoryTarget, PurchaseInput
from app.services.shopping.workflow import (
    RemovalInput,
    ResolvedShoppingChanges,
    ShoppingWriteBuilder,
)


class TimeoutAfterCommitContainer:
    """Inject one synthetic response loss after Cosmos commits an operation batch."""

    def __init__(self, container):
        self._container = container
        self.lose_next_receipt_batch_response = False
        self.timeout_injected = False

    def __getattr__(self, name):
        return getattr(self._container, name)

    def execute_item_batch(self, *, batch_operations, partition_key, response_hook):
        result = self._container.execute_item_batch(
            batch_operations=batch_operations,
            partition_key=partition_key,
            response_hook=response_hook,
        )
        has_receipt = any(
            operation[0] == "create" and operation[1][0].get("record_type") == "operation_receipt"
            for operation in batch_operations
        )
        if self.lose_next_receipt_batch_response and has_receipt:
            self.lose_next_receipt_batch_response = False
            self.timeout_injected = True
            raise TimeoutError("Synthetic response loss after Cosmos commit.")
        return result


def progress(message):
    print(f"[B1 Cosmos acceptance] {message}", flush=True)


class AcceptanceResolver:
    """Use A1's resolver for existing purchases; adapt only unsupported test cases."""

    def __init__(self, reader, purchase_resolver):
        self.reader = reader
        self.purchase_resolver = purchase_resolver

    async def resolve(self, record):
        purchases, removals, attachments = [], [], []
        approved_ids = set(record.approved_operation_ids or ())
        production_operations = [
            operation
            for operation in record.operations
            if operation.operation_id in approved_ids
            and operation.action == "complete_purchase"
            and "item_id" in operation.values
        ]
        if production_operations:
            purchase_record = record.model_copy(
                update={
                    "operations": production_operations,
                    "approved_operation_ids": [
                        operation.operation_id for operation in production_operations
                    ],
                }
            )
            resolved = await self.purchase_resolver.resolve(purchase_record)
            purchases.extend(resolved.purchases)

        for operation in record.operations:
            if operation.operation_id not in approved_ids or operation in production_operations:
                continue
            values = operation.values
            if operation.action == "remove_shopping":
                shopping_doc = await self.reader.read_item(
                    item_id=f"shopping_item:{values['shopping_item_id']}",
                    partition_key=record.household_id,
                )
                shopping = CosmosDocumentMapper.from_document(
                    ShoppingItem, shopping_doc, household_id=record.household_id
                )
                versions = {target.record_id: target.etag for target in operation.targets}
                if versions.get(shopping_doc["id"]) is None:
                    raise ValueError("Synthetic removal card is missing its Shopping ETag")
                removals.append(
                    RemovalInput(
                        operation_id=operation.operation_id,
                        shopping=shopping,
                        shopping_etag=versions[shopping_doc["id"]],
                    )
                )
                continue
            if operation.action != "complete_purchase" or "item_id" in values:
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
                    inventory=None,
                    inventory_etag=None,
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
            purchases=tuple(purchases),
            removals=tuple(removals),
            media_attachments=tuple(attachments),
        )


async def verify(args, container):
    household = args.household
    isolation_household = f"{household}-isolation"
    container = TimeoutAfterCommitContainer(container)
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
    read_repository = CosmosReadRepository(reader)
    storage = CosmosMediaMappingStorage(read_repository=read_repository, write_client=writer)
    builder = ShoppingWriteBuilder(
        reader,
        AcceptanceResolver(reader, CardInputResolver(read_repository)),
        media_retention=B1MediaRetentionPlanner(storage),
    )
    service = ApprovalService(repo, builder)
    now = datetime.now(UTC)
    checks = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        progress(f"PASS {label}")

    async def read(identity):
        return await reader.read_item(item_id=identity, partition_key=household)

    async def records(kind):
        return await reader.query_items(
            query="SELECT * FROM c WHERE c.household_id = @home AND c.record_type = @kind",
            parameters=[{"name": "@home", "value": household}, {"name": "@kind", "value": kind}],
            partition_key=household,
        )

    progress("Checking synthetic household partitions and container TTL before writes.")
    # Refuse reuse of either synthetic partition before writing any test data.
    for candidate in (household, isolation_household):
        existing = await reader.query_items(
            query="SELECT TOP 1 * FROM c WHERE c.household_id = @home",
            parameters=[{"name": "@home", "value": candidate}],
            partition_key=candidate,
        )
        if existing:
            raise ValueError(f"Synthetic household {candidate!r} is not empty; use a new ID")
    if container.read().get("defaultTtl") != -1:
        raise ValueError("Container TTL not configured; no settings were changed")
    progress("Preflight passed; seeding synthetic fixtures.")
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
    removable_shopping = ShoppingItem(
        shopping_item_id="remove-me",
        household_id=household,
        name="合成待購項目",
        quantity=1,
        unit="個",
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
            for record in (stock, shopping, new_shopping, removable_shopping, media)
        ),
    )
    isolated_stock = stock.model_copy(update={"household_id": isolation_household, "quantity": 999})
    await writer.execute_batch(
        partition_key=isolation_household,
        operations=(CreateDocument(CosmosDocumentMapper.to_document(isolated_stock)),),
    )
    inventory_doc, shopping_doc = (
        await read("inventory_item:tissue"),
        await read("shopping_item:tissue"),
    )
    check("synthetic_seed", inventory_doc["quantity"] == 1 and shopping_doc["quantity"] == 2)
    isolated_read = await reader.read_item(
        item_id="inventory_item:tissue", partition_key=isolation_household
    )
    check(
        "household_partition_isolation",
        inventory_doc["quantity"] == 1
        and isolated_read is not None
        and isolated_read["quantity"] == 999,
    )

    marker_id = "synthetic_acceptance:atomicity-probe"
    log_stream = io.StringIO()
    cosmos_logger = logging.getLogger("app.services.cosmos_sdk")
    log_handler = logging.StreamHandler(log_stream)
    old_level = cosmos_logger.level
    cosmos_logger.setLevel(logging.WARNING)
    cosmos_logger.addHandler(log_handler)
    try:
        try:
            await writer.execute_batch(
                partition_key=household,
                operations=(
                    CreateDocument(
                        {
                            "id": marker_id,
                            "record_type": "synthetic_acceptance_probe",
                            "household_id": household,
                        }
                    ),
                    ReplaceDocument(
                        document={**inventory_doc, "quantity": 999},
                        if_match_etag='"stale-etag"',
                    ),
                ),
            )
        except BatchOperationConflict as conflict:
            check("etag_conflict_detected", conflict.operation_index == 1)
        else:
            check("etag_conflict_detected", False)
    finally:
        cosmos_logger.removeHandler(log_handler)
        cosmos_logger.setLevel(old_level)
    failure_log = log_stream.getvalue()
    log_handler.close()
    check(
        "batch_all_or_none_on_etag_conflict",
        await read(marker_id) is None and (await read("inventory_item:tissue"))["quantity"] == 1,
    )
    check(
        "cosmos_error_metrics_observed_without_document_data",
        all(
            field in failure_log
            for field in (
                "request_charge=",
                "operation_count=2",
                "operation_index=1",
                "status_code=412",
            )
        )
        and household not in failure_log
        and marker_id not in failure_log,
    )

    retry_operation_id = f"synthetic-retry-{household}"
    retry_effect = CreateDocument(
        {
            "id": "synthetic_acceptance:retry-probe",
            "record_type": "synthetic_acceptance_probe",
            "household_id": household,
        }
    )
    first_write = await repo.execute_idempotent_batch(
        household_id=household,
        operation_id=retry_operation_id,
        operations=(retry_effect,),
    )
    retry_write = await repo.execute_idempotent_batch(
        household_id=household,
        operation_id=retry_operation_id,
        operations=(retry_effect,),
    )
    check(
        "same_operation_retry_is_idempotent",
        first_write is WriteStatus.APPLIED
        and retry_write is WriteStatus.ALREADY_APPLIED
        and await read("synthetic_acceptance:retry-probe") is not None,
    )
    changed_retry_effect = CreateDocument(
        {
            "id": "synthetic_acceptance:changed-retry-probe",
            "record_type": "synthetic_acceptance_probe",
            "household_id": household,
            "payload_variant": "changed",
        }
    )
    try:
        await repo.execute_idempotent_batch(
            household_id=household,
            operation_id=retry_operation_id,
            operations=(changed_retry_effect,),
        )
    except OperationIdConflict:
        check("same_operation_changed_payload_is_rejected", True)
    else:
        check("same_operation_changed_payload_is_rejected", False)
    check(
        "changed_operation_retry_has_no_side_effect",
        await read("synthetic_acceptance:changed-retry-probe") is None,
    )

    partial_operation_id = f"synthetic-partial-{household}"
    missing_operation_id = f"synthetic-missing-{household}"
    partial_effect = CreateDocument(
        {
            "id": "synthetic_acceptance:partial-effect",
            "record_type": "synthetic_acceptance_probe",
            "household_id": household,
        }
    )
    missing_effect = CreateDocument(
        {
            "id": "synthetic_acceptance:must-not-write",
            "record_type": "synthetic_acceptance_probe",
            "household_id": household,
        }
    )
    await repo.execute_idempotent_batch(
        household_id=household,
        operation_id=partial_operation_id,
        operations=(partial_effect,),
    )
    try:
        await repo.execute_idempotent_batches(
            household_id=household,
            operations_by_id={
                partial_operation_id: (partial_effect,),
                missing_operation_id: (missing_effect,),
            },
        )
    except OperationReceiptInconsistency as error:
        check(
            "receipt_mismatch_reports_reconciliation_required",
            "reconciliation is required" in str(error),
        )
    else:
        check("receipt_mismatch_reports_reconciliation_required", False)
    check(
        "receipt_mismatch_stops_all_new_writes",
        await read("synthetic_acceptance:must-not-write") is None
        and await repo.read_operation_receipt(
            household_id=household, operation_id=missing_operation_id
        )
        is None,
    )
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
        "pending_approval_ttl",
        (await read(f"pending_approval:{card.approval_id}"))["ttl"]
        == int((timedelta(minutes=30) + timedelta(hours=24)).total_seconds()),
    )
    check(
        "stock_unchanged_before_confirmation",
        (await read("inventory_item:tissue"))["quantity"] == 1,
    )
    request = ResumeRequest(household_id=household, approval_id=card.approval_id, approved=True)
    result = await service.resume(request)
    check("confirmation_completed", result.outcome == ResumeOutcome.COMPLETED)
    stored_card = await read(f"pending_approval:{card.approval_id}")
    check("completed_approval_ttl", stored_card["ttl"] == -1)
    check("stock_now_three", (await read("inventory_item:tissue"))["quantity"] == 3)
    check("shopping_completed", (await read("shopping_item:tissue"))["status"] == "completed")
    tx = await records("transaction")
    check("one_actual_purchase_transaction", len(tx) == 1 and tx[0]["quantity_delta"] == 2)
    check(
        "missing_event_date_uses_first_decision_time",
        datetime.fromisoformat(tx[0]["created_at"]) == result.record.decided_at,
    )
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
    shopping_doc, media_doc = await read("shopping_item:soap"), await read("input:synthetic-media")

    def new_soap_operation(transaction_id):
        return NewOperation(
            action="complete_purchase",
            description="合成測試：會新增洗碗精 1 瓶，附加合成媒體",
            values={
                "name": "洗碗精",
                "purchased_quantity": 1,
                "purchased_unit": "瓶",
                "shopping_item_id": "soap",
                "transaction_id": transaction_id,
                "location": "廚房",
                "image_ref": image_ref,
            },
            targets=[
                TargetVersion(record_id=doc["id"], etag=doc["_etag"])
                for doc in (shopping_doc, media_doc)
            ],
        )

    cancelled_card = await service.save(
        service.prepare(
            household_id=household,
            presentation=ApprovalPresentation.SINGLE_ACTION,
            operations=[new_soap_operation("tx-soap-cancelled")],
        )
    )
    cancelled_operation_id = cancelled_card.operation_ids[0]
    cancelled_item_id = resolve_inventory_item_id(cancelled_operation_id)
    cancelled_result = await service.resume(
        ResumeRequest(
            household_id=household,
            approval_id=cancelled_card.approval_id,
            approved=False,
        )
    )
    cancelled_document = await read(f"pending_approval:{cancelled_card.approval_id}")
    check(
        "cancelled_approval_recorded",
        cancelled_result.outcome == ResumeOutcome.CANCELLED
        and cancelled_document["status"] == "cancelled"
        and cancelled_document["cancel_reason"] == "user"
        and cancelled_document["ttl"] == int(timedelta(hours=24).total_seconds()),
    )
    check(
        "cancelled_new_item_has_no_domain_effects",
        await read(f"inventory_item:{cancelled_item_id}") is None
        and (await read("shopping_item:soap"))["status"] == "pending"
        and len(await records("inventory_item")) == 1
        and len(await records("transaction")) == 1
        and len(await records("change_event")) == 1
        and await repo.read_operation_receipt(
            household_id=household, operation_id=cancelled_operation_id
        )
        is None
        and (await read("input:synthetic-media"))["ttl"] == 86400,
    )

    removal_doc = await read("shopping_item:remove-me")
    removal_card = await service.save(
        service.prepare(
            household_id=household,
            presentation=ApprovalPresentation.SINGLE_ACTION,
            operations=[
                NewOperation(
                    action="remove_shopping",
                    description="合成測試：從待購清單移除項目",
                    values={"name": "合成待購項目", "shopping_item_id": "remove-me"},
                    targets=[TargetVersion(record_id=removal_doc["id"], etag=removal_doc["_etag"])],
                )
            ],
        )
    )
    removal_operation_id = removal_card.operation_ids[0]
    removal_result = await service.resume(
        ResumeRequest(
            household_id=household,
            approval_id=removal_card.approval_id,
            approved=True,
        )
    )
    removed_doc = await read("shopping_item:remove-me")
    check(
        "shopping_removal_writes_storage_tombstone",
        removal_result.outcome == ResumeOutcome.COMPLETED
        and removed_doc["status"] == "pending"
        and removed_doc["deleted_at"] == removal_result.record.decided_at.isoformat(),
    )
    check(
        "shopping_removal_is_hidden_from_active_list",
        all(
            item.shopping_item_id != "remove-me"
            for item in await CosmosReadRepository(reader).list_shopping(household)
        ),
    )
    check(
        "shopping_removal_retry_has_no_duplicate_effects",
        (
            await service.resume(
                ResumeRequest(
                    household_id=household,
                    approval_id=removal_card.approval_id,
                    approved=True,
                )
            )
        ).outcome
        == ResumeOutcome.ALREADY_COMPLETED
        and await repo.read_operation_receipt(
            household_id=household, operation_id=removal_operation_id
        )
        is not None
        and (await read("shopping_item:remove-me"))["deleted_at"]
        == removal_result.record.decided_at.isoformat(),
    )
    check(
        "shopping_removal_does_not_change_inventory_or_transactions",
        (await read("inventory_item:tissue"))["quantity"] == 3
        and (await read("inventory_item:remove-me")) is None
        and len(await records("transaction")) == 1
        and len(await records("change_event")) == 1,
    )

    # New Inventory + real B1 stable ID + real B1 mapping-retention helper in the same batch.
    new_card = await service.save(
        service.prepare(
            household_id=household,
            presentation=ApprovalPresentation.SINGLE_ACTION,
            operations=[new_soap_operation("tx-soap")],
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
    container.lose_next_receipt_batch_response = True
    timeout_result = await service.resume(new_request)
    check(
        "timeout_after_commit_recovers_completed_purchase",
        container.timeout_injected and timeout_result.outcome == ResumeOutcome.ALREADY_COMPLETED,
    )
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
        "scope": (
            "B1 SDK/repository + C1 builder + production A1 resolver for existing-item "
            "purchases; test adapters for new-item/removal cases; not Agent/App E2E"
        ),
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
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=20,
        help="Per-request Cosmos timeout; defaults to 20 seconds.",
    )
    parser.add_argument(
        "--overall-timeout-seconds",
        type=int,
        default=300,
        help="Maximum acceptance duration; defaults to 300 seconds.",
    )
    args = parser.parse_args()
    if (
        not args.execute
        or not args.household.startswith("c1-test-")
        or args.household == "demo-household"
    ):
        parser.error("Requires --execute and a new c1-test-* household")
    if args.request_timeout_seconds <= 0 or args.overall_timeout_seconds <= 0:
        parser.error("Timeout values must be positive integers.")
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
    credential = AzureCliCredential(process_timeout=max(60, args.request_timeout_seconds))
    client = None
    try:
        progress(f"Connecting with bounded timeouts to synthetic household {args.household!r}.")
        client = CosmosClient(
            args.endpoint,
            credential=credential,
            timeout=args.request_timeout_seconds,
            connection_timeout=args.request_timeout_seconds,
            read_timeout=args.request_timeout_seconds,
            retry_total=1,
            retry_connect=1,
            retry_read=1,
            retry_status=1,
            retry_backoff_max=1,
        )
        container = client.get_database_client("nestock").get_container_client("household-data")
        progress("Cosmos client initialized; starting acceptance checks.")
        result = asyncio.run(
            asyncio.wait_for(
                verify(args, container),
                timeout=args.overall_timeout_seconds,
            )
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except (TimeoutError, CosmosClientTimeoutError) as error:
        raise SystemExit(
            "Cosmos acceptance timed out; the synthetic household may contain partial data. "
            "Inspect it read-only and use a new household ID."
        ) from error
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
