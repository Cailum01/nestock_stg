"""C1 batch builder; A1 resolves stored values, ApprovalService authorizes writes.

No route is enabled here. The injected resolver is trusted backend code, never
a decoder of client-provided values. It must reject unsupported actions and bind
every quantity, unit, target, timestamp and stable ID to the server-stored card.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.models.agent import ApprovalPresentation
from app.models.domain import ChangeEvent, InventoryItem, ShoppingItem
from app.services.approvals import (
    MAX_BATCH_OPERATIONS,
    ApprovalRecord,
    ApprovalStatus,
    BatchTooLarge,
    DomainWritePlan,
    TargetChanged,
)
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_write import CreateDocument, ReplaceDocument, change_event_create
from app.services.shopping.media import MediaAttachment, MediaRetentionPlanner
from app.services.shopping.planning import AdjustmentInput, PurchaseInput, ShoppingPlanService
from app.services.shopping.preview import ShoppingDecisionRequired, ShoppingPreviewError


@dataclass(frozen=True)
class RemovalInput:
    operation_id: str
    shopping: ShoppingItem
    shopping_etag: str


@dataclass(frozen=True)
class ResolvedShoppingChanges:
    purchases: tuple[PurchaseInput, ...] = ()
    removals: tuple[RemovalInput, ...] = ()
    adjustments: tuple[AdjustmentInput, ...] = ()
    media_attachments: tuple[MediaAttachment, ...] = ()


class ShoppingInputResolver(Protocol):
    """A1's typed stored-proposal handoff, still requiring A1 integration.

    Resolve only approved_operation_ids using exact stored values and stable IDs.
    Do not re-match or change values on retry. ApprovalService's pending save-time
    trial also carries selection and a trial decided_at; planning must do no I/O
    writes and must not treat that trial as authorization.
    """

    async def resolve(self, record: ApprovalRecord) -> ResolvedShoppingChanges: ...


class ShoppingDocumentReader(Protocol):
    async def read_item(
        self, *, item_id: str, partition_key: str
    ) -> Mapping[str, object] | None: ...


def _replacement(source, record):
    # Retain storage-only metadata; Cosmos-managed properties are never written.
    document = {key: value for key, value in source.items() if not key.startswith("_")}
    document.update(CosmosDocumentMapper.to_document(record))
    return document


class ShoppingWriteBuilder:
    """Plan C1 purchase/removal/adjustment writes; never execute or authorize."""

    ITEM_MATCH_SAME_ITEM = "same_item"

    def __init__(
        self,
        reader: ShoppingDocumentReader,
        resolver: ShoppingInputResolver,
        *,
        media_retention: MediaRetentionPlanner | None = None,
    ):
        self._reader = reader
        self._resolver = resolver
        self._plans = ShoppingPlanService()
        self._media_retention = media_retention

    async def build(self, record: ApprovalRecord) -> DomainWritePlan:
        if record.status not in (ApprovalStatus.PENDING, ApprovalStatus.CONFIRMED):
            raise ShoppingPreviewError("Only a pending trial or confirmed card can be planned.")
        if record.presentation not in (
            ApprovalPresentation.SINGLE_ACTION,
            ApprovalPresentation.BATCH_ACTION,
            ApprovalPresentation.ITEM_MATCH,
        ):
            raise ShoppingPreviewError("This builder handles resolved domain-change cards only.")
        if record.decided_at is None:
            raise ShoppingPreviewError("A fixed confirmation time is required.")
        if record.presentation is ApprovalPresentation.ITEM_MATCH:
            option_ids = {option.option_id for option in record.options}
            if (
                record.selected_option_id != self.ITEM_MATCH_SAME_ITEM
                or self.ITEM_MATCH_SAME_ITEM not in option_ids
            ):
                raise ShoppingDecisionRequired(
                    "Only a confirmed same-item match can enter the purchase writer."
                )
            if len(record.operations) != 1:
                raise ShoppingPreviewError("An item-match purchase must contain one operation.")
            selected = record.operation_ids
        else:
            selected = record.approved_operation_ids or []
        if not selected or len(set(selected)) != len(selected):
            raise ShoppingPreviewError("A non-empty, unique fixed selection is required.")
        stored = {op.operation_id: op for op in record.operations}
        if len(stored) != len(record.operations) or not set(selected) <= stored.keys():
            raise ShoppingPreviewError("Selection must belong to the stored card.")
        changes = await self._resolver.resolve(record)
        inputs = (*changes.purchases, *changes.removals, *changes.adjustments)
        ids = [entry.operation_id for entry in inputs]
        if len(set(ids)) != len(ids) or set(ids) != set(selected):
            raise ShoppingPreviewError("Resolved operations must exactly match fixed selection.")
        if record.presentation is ApprovalPresentation.ITEM_MATCH:
            if len(changes.purchases) != 1 or changes.removals or changes.adjustments:
                raise ShoppingPreviewError("A same-item match must resolve to one purchase.")
            self._validate_item_match_purchase(
                stored[changes.purchases[0].operation_id], changes.purchases[0]
            )
        order = {op.operation_id: index for index, op in enumerate(record.operations)}
        changes = ResolvedShoppingChanges(
            purchases=tuple(sorted(changes.purchases, key=lambda row: order[row.operation_id])),
            removals=tuple(sorted(changes.removals, key=lambda row: order[row.operation_id])),
            adjustments=tuple(sorted(changes.adjustments, key=lambda row: order[row.operation_id])),
            media_attachments=changes.media_attachments,
        )
        shopping_ids = [
            entry.shopping.shopping_item_id for entry in (*changes.purchases, *changes.removals)
        ]
        if len(set(shopping_ids)) != len(shopping_ids):
            raise ShoppingPreviewError("A Shopping target can be changed only once per card.")
        purchase_transaction_ids = {entry.transaction_id for entry in changes.purchases}
        purchase_item_ids = {
            item_id
            for entry in changes.purchases
            for item_id in (
                entry.inventory.item_id if entry.inventory is not None else None,
                entry.new_inventory.item_id if entry.new_inventory is not None else None,
            )
            if item_id is not None
        }

        for adjustment in changes.adjustments:
            operation = stored[adjustment.operation_id]
            values = operation.values
            target = adjustment.new_inventory
            if (
                operation.action != "create_item"
                or operation.targets
                or adjustment.approval_id != record.approval_id
                or adjustment.transaction_id in purchase_transaction_ids
                or target.item_id in purchase_item_ids
                or values.get("transaction_id") != adjustment.transaction_id
                or values.get("name") != target.name
                or values.get("quantity") != adjustment.quantity
                or values.get("unit") != target.unit
                or values.get("location") != target.location
            ):
                raise ShoppingPreviewError(
                    "Adjustment inputs must match the exact stored create_item proposal."
                )

        attachments = {entry.operation_id: entry for entry in changes.media_attachments}
        purchase_ids = {entry.operation_id for entry in changes.purchases}
        if (
            len(attachments) != len(changes.media_attachments)
            or not attachments.keys() <= purchase_ids
        ):
            raise ShoppingPreviewError("One attachment may belong to each selected purchase only.")
        if attachments and self._media_retention is None:
            raise ShoppingDecisionRequired(
                "Configure the B1 retention port before attaching media."
            )
        for operation_id, attachment in attachments.items():
            if stored[operation_id].values.get("image_ref") != attachment.image_ref:
                raise ShoppingPreviewError(
                    "Attachment must match the image_ref on the stored card."
                )

        documents = {}
        for entry in (*changes.purchases, *changes.removals):
            expected = {f"shopping_item:{entry.shopping.shopping_item_id}": entry.shopping_etag}
            sources = [(ShoppingItem, entry.shopping, entry.shopping_etag)]
            if isinstance(entry, PurchaseInput) and entry.inventory is not None:
                expected[f"inventory_item:{entry.inventory.item_id}"] = entry.inventory_etag
                sources.append((InventoryItem, entry.inventory, entry.inventory_etag))
            attachment = attachments.get(entry.operation_id)
            if attachment is not None:
                expected[f"input:{attachment.input_id}"] = attachment.mapping_etag
            targets = stored[entry.operation_id].targets
            if (
                len(targets) != len(expected)
                or {target.record_id: target.etag for target in targets} != expected
            ):
                raise ShoppingPreviewError("Inputs must use exactly the card's target ETags.")
            for model, source, etag in sources:
                doc_id = CosmosDocumentMapper.to_document(source)["id"]
                document = await self._reader.read_item(
                    item_id=doc_id, partition_key=record.household_id
                )
                if (
                    document is None
                    or document.get("_etag") != etag
                    or document.get("deleted_at") is not None
                ):
                    raise TargetChanged(doc_id)
                current = CosmosDocumentMapper.from_document(
                    model, document, household_id=record.household_id
                )
                if current != source:
                    raise ShoppingPreviewError("Resolved snapshots must match stored documents.")
                if doc_id in documents and documents[doc_id] != document:
                    raise TargetChanged(doc_id)
                documents[doc_id] = document
            if isinstance(entry, PurchaseInput) and entry.new_inventory is not None:
                doc_id = f"inventory_item:{entry.new_inventory.item_id}"
                if await self._reader.read_item(item_id=doc_id, partition_key=record.household_id):
                    raise TargetChanged(doc_id)

        by_id, shared, outcomes = {}, [], {}
        if changes.purchases:
            plan = self._plans.plan_purchase(
                household_id=record.household_id,
                purchases=changes.purchases,
                recorded_at=record.decided_at,
            )
            transactions = {tx.operation_id: tx for tx in plan.transactions}
            images_by_item = {}
            retention_by_id = {}
            for purchase in changes.purchases:
                attachment = attachments.get(purchase.operation_id)
                if attachment is None:
                    continue
                tx = transactions[purchase.operation_id]
                previous = images_by_item.get(tx.item_id)
                if previous is not None and previous != attachment.image_ref:
                    raise ShoppingPreviewError(
                        "A shared Inventory target needs one confirmed image."
                    )
                if purchase.inventory is not None and purchase.inventory.image_ref not in (
                    None,
                    attachment.image_ref,
                ):
                    raise ShoppingDecisionRequired(
                        "Replacing media needs an agreed detach/remaining-reference policy."
                    )
                images_by_item[tx.item_id] = attachment.image_ref
                operation = await self._media_retention.prepare_attachment(
                    household_id=record.household_id, attachment=attachment
                )
                doc_id = f"input:{attachment.input_id}"
                if (
                    not isinstance(operation, ReplaceDocument)
                    or operation.document.get("id") != doc_id
                    or operation.document.get("household_id") != record.household_id
                    or operation.document.get("record_type") != "user_input"
                    or operation.document.get("image_ref") != attachment.image_ref
                    or operation.document.get("status") != "completed"
                    or operation.document.get("ttl", -1) != -1
                    or operation.if_match_etag != attachment.mapping_etag
                ):
                    raise ShoppingPreviewError("Retention update must match the completed mapping.")
                if doc_id in retention_by_id and retention_by_id[doc_id] != operation:
                    raise ShoppingPreviewError("Shared media mapping updates must agree.")
                retention_by_id[doc_id] = operation

            def with_image(item):
                image_ref = images_by_item.get(item.item_id)
                return (
                    item if image_ref is None else item.model_copy(update={"image_ref": image_ref})
                )

            creates = {row.operation_id: with_image(row.record) for row in plan.inventory_creates}
            after_by_item = {row.record.item_id: row.record for row in plan.inventory_updates}
            after_by_item.update({row.record.item_id: row.record for row in plan.inventory_creates})
            for update in plan.shopping_updates:
                tx = transactions[update.operation_id]
                doc_id = f"shopping_item:{update.record.shopping_item_id}"
                event = ChangeEvent(
                    event_id=tx.operation_id,
                    household_id=record.household_id,
                    operation_id=tx.operation_id,
                    approval_id=record.approval_id,
                    transaction_id=tx.transaction_id,
                    shopping_item_id=update.record.shopping_item_id,
                    item_id=tx.item_id,
                    occurred_at=tx.created_at,
                    recorded_at=record.decided_at,
                )
                writes = [
                    ReplaceDocument(
                        _replacement(documents[doc_id], update.record), update.expected_etag
                    ),
                    CreateDocument(CosmosDocumentMapper.to_document(tx)),
                    change_event_create(event),
                ]
                if tx.operation_id in creates:
                    writes.append(
                        CreateDocument(CosmosDocumentMapper.to_document(creates[tx.operation_id]))
                    )
                by_id[tx.operation_id] = tuple(writes)
                outcomes[tx.operation_id] = {
                    "shopping_item_id": update.record.shopping_item_id,
                    "item_id": tx.item_id,
                    "transaction_id": tx.transaction_id,
                    "quantity_added": tx.quantity_delta,
                    "inventory_quantity": after_by_item[tx.item_id].quantity,
                    "unit": after_by_item[tx.item_id].unit,
                }
            shared = [
                ReplaceDocument(
                    _replacement(
                        documents[f"inventory_item:{update.record.item_id}"],
                        with_image(update.record),
                    ),
                    update.expected_etag,
                )
                for update in plan.inventory_updates
            ]
            shared.extend(retention_by_id.values())
        if changes.adjustments:
            adjustment_ids: set[str] = set()
            transaction_ids: set[str] = set()
            item_ids: set[str] = set()
            for adjustment in changes.adjustments:
                if (
                    adjustment.operation_id in adjustment_ids
                    or adjustment.transaction_id in transaction_ids
                    or adjustment.new_inventory.item_id in item_ids
                ):
                    raise ShoppingPreviewError(
                        "Adjustment operations, transactions and new items must be unique."
                    )
                adjustment_ids.add(adjustment.operation_id)
                transaction_ids.add(adjustment.transaction_id)
                item_ids.add(adjustment.new_inventory.item_id)
                plan = self._plans.plan_adjustment(
                    household_id=record.household_id,
                    adjustment=adjustment,
                    recorded_at=record.decided_at,
                )
                doc_id = f"inventory_item:{plan.inventory_create.record.item_id}"
                if await self._reader.read_item(item_id=doc_id, partition_key=record.household_id):
                    raise TargetChanged(doc_id)
                by_id[adjustment.operation_id] = (
                    CreateDocument(CosmosDocumentMapper.to_document(plan.inventory_create.record)),
                    CreateDocument(CosmosDocumentMapper.to_document(plan.transaction)),
                    change_event_create(plan.change_event),
                )
                outcomes[adjustment.operation_id] = {
                    "item_id": plan.inventory_create.record.item_id,
                    "transaction_id": plan.transaction.transaction_id,
                    "quantity_added": plan.transaction.quantity_delta,
                    "inventory_quantity": plan.inventory_create.record.quantity,
                    "unit": plan.inventory_create.record.unit,
                }
        for entry in changes.removals:
            removal = self._plans.plan_removal(
                household_id=record.household_id,
                operation_id=entry.operation_id,
                shopping=entry.shopping,
                shopping_etag=entry.shopping_etag,
                recorded_at=record.decided_at,
            )
            doc_id = f"shopping_item:{removal.shopping_item_id}"
            document = _replacement(documents[doc_id], entry.shopping)
            document["deleted_at"] = removal.deleted_at.isoformat()
            by_id[entry.operation_id] = (ReplaceDocument(document, removal.expected_etag),)
            outcomes[entry.operation_id] = {"shopping_item_id": removal.shopping_item_id}

        # Includes B1 receipts and A1's final conditional completed-card replace.
        if len(by_id) + sum(map(len, by_id.values())) + len(shared) + 1 > MAX_BATCH_OPERATIONS:
            raise BatchTooLarge("Shopping changes and approval do not fit one atomic batch.")
        return DomainWritePlan(
            operations_by_id=by_id, shared_operations=tuple(shared), outcome=outcomes
        )

    @staticmethod
    def _validate_item_match_purchase(operation, purchase: PurchaseInput) -> None:
        """Bind a match-card purchase to its displayed, stored values.

        A `same_item` answer authorizes the purchase shown on that card. Matching
        does not persist an alias; that would require a separately displayed and
        approved write under the current C1/B1 decision.
        """
        values = operation.values
        quantity = values.get("quantity")
        if (
            operation.action != "confirm_item_match"
            or purchase.inventory is None
            or not isinstance(values.get("user_term"), str)
            or not values.get("user_term", "").strip()
            or values.get("candidate_item_id") != purchase.inventory.item_id
            or values.get("candidate_name") != purchase.inventory.name
            or values.get("quantity") != purchase.purchased_quantity
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not isinstance(values.get("unit"), str)
            or values.get("unit") != purchase.purchased_unit
            or values.get("transaction_id") != purchase.transaction_id
            or values.get("shopping_item_id") != purchase.shopping.shopping_item_id
            or purchase.shopping.name != values.get("user_term")
            or purchase.shopping.item_id is not None
            or purchase.resolved_inventory_item_id != purchase.inventory.item_id
        ):
            raise ShoppingPreviewError(
                "Matched purchase inputs must match the exact stored item-match proposal."
            )
