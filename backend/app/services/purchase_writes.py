"""A1 adapter for 買回來了 (v8 flow C): a confirmed purchase card is written through
C1's purchase plan into one B1 batch (Issue #60).

Each card line is fixed when the card is made (A1 in Issue #60): the shopping entry
(`shopping_item_id`), the inventory item it adds to (`item_id`), `purchased_quantity`,
`purchased_unit`, a `transaction_id`, and the version (ETag) of both records.
`purchased_at` is not on the card yet, so `decided_at` stands in for it (Issue #60).
`/agent/resume` only picks the card and its lines; it never supplies values.

At confirmation `PurchaseWriteBuilder` reads both records again. If either changed
since the card was made, it raises `TargetChanged` and nothing is written; the
approval service then cancels the card, and a new card is needed. Otherwise
C1's `ShoppingPlanService.plan_purchase` makes the plan, which is mapped the way B1
agreed in Issue #60:

- per operation: the shopping entry replace, the Transaction create and the change
  event create;
- shared: one inventory replace per item, merged when two entries add to the same
  item.

Not covered yet: a purchase time other than the confirmation time (`decided_at`
fills Transaction `created_at`), items that are not on the list or not linked to an
inventory item (Issue #90), buying more than listed, and a different unit. C1's
preview refuses those when the card is made.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from app.models.domain import ChangeEvent, InventoryItem, ShoppingItem
from app.services.approvals import (
    ApprovalRecord,
    DomainWritePlan,
    NewOperation,
    StoredOperation,
    TargetChanged,
    TargetVersion,
)
from app.services.cosmos_read import CosmosDocumentMapper, MappedDocument
from app.services.cosmos_write import (
    BatchOperation,
    CreateDocument,
    ReplaceDocument,
    change_event_create,
)
from app.services.shopping.planning import PurchaseInput, ShoppingPlanService
from app.services.shopping.preview import ShoppingPreviewService

COMPLETE_PURCHASE = "complete_purchase"
"""`action` of a purchase card line (v8 tool `complete_purchase`)."""


class UnsupportedAction(ValueError):
    """The card has an operation this builder does not write."""


class PurchaseRecordReader(Protocol):
    """The part of B1's `CosmosReadRepository` this adapter uses (PR #72)."""

    async def read_shopping_item_with_etag(
        self, *, household_id: str, shopping_item_id: str
    ) -> MappedDocument[ShoppingItem] | None: ...

    async def read_inventory_item_with_etag(
        self, *, household_id: str, item_id: str
    ) -> MappedDocument[InventoryItem] | None: ...


def purchase_operation(
    *,
    shopping: MappedDocument[ShoppingItem],
    inventory: MappedDocument[InventoryItem] | None,
    quantity: int,
    transaction_id: str | None = None,
) -> NewOperation:
    """One card line: "this shopping entry was bought, add it to this inventory item".

    C1's preview checks the line first, so a purchase the plan would refuse (more than
    listed, not linked to an inventory item, a different unit) never reaches a card;
    it raises `ShoppingDecisionRequired` or `ShoppingPreviewError`.
    """
    preview = ShoppingPreviewService().preview_purchase(
        household_id=shopping.record.household_id,
        shopping=shopping.record,
        inventory=None if inventory is None else inventory.record,
        purchased_quantity=quantity,
    )
    if inventory is None or shopping.etag is None or inventory.etag is None:
        raise ValueError("Both records must be read with their ETag.")
    name = shopping.record.name
    return NewOperation(
        action=COMPLETE_PURCHASE,
        description=f"{name} +{quantity} {preview.unit}（從待購清單完成購買）",
        values={
            "name": name,
            "purchased_quantity": quantity,
            "purchased_unit": preview.unit,
            "shopping_item_id": shopping.record.shopping_item_id,
            "item_id": inventory.record.item_id,
            "transaction_id": transaction_id or uuid.uuid4().hex,
        },
        targets=[
            TargetVersion(record_id=shopping.document_id, etag=shopping.etag),
            TargetVersion(record_id=inventory.document_id, etag=inventory.etag),
        ],
    )


class PurchaseWriteBuilder:
    """`DomainWriteBuilder` for purchase cards (A1 adapter, Issue #60)."""

    def __init__(
        self,
        reader: PurchaseRecordReader,
        planner: ShoppingPlanService | None = None,
    ) -> None:
        self._reader = reader
        self._planner = planner or ShoppingPlanService()

    async def build(self, record: ApprovalRecord) -> DomainWritePlan:
        if record.decided_at is None:
            raise ValueError("A card is planned only with its decision time.")
        approved = set(record.approved_operation_ids or ())
        operations = [op for op in record.operations if op.operation_id in approved]
        purchases = [await self._purchase_input(record, op) for op in operations]

        plan = self._planner.plan_purchase(
            household_id=record.household_id,
            purchases=purchases,
            recorded_at=record.decided_at,
        )
        shopping_by_id = {update.operation_id: update for update in plan.shopping_updates}
        after_by_item = {update.record.item_id: update.record for update in plan.inventory_updates}

        operations_by_id: dict[str, Sequence[BatchOperation]] = {}
        outcome: dict[str, dict[str, object]] = {}
        for transaction in plan.transactions:
            shopping = shopping_by_id[transaction.operation_id]
            event = ChangeEvent(
                event_id=transaction.operation_id,
                household_id=record.household_id,
                operation_id=transaction.operation_id,
                approval_id=record.approval_id,
                transaction_id=transaction.transaction_id,
                shopping_item_id=shopping.record.shopping_item_id,
                item_id=transaction.item_id,
                occurred_at=transaction.created_at,
                recorded_at=record.decided_at,
            )
            operations_by_id[transaction.operation_id] = [
                ReplaceDocument(
                    CosmosDocumentMapper.to_document(shopping.record),
                    if_match_etag=shopping.expected_etag,
                ),
                CreateDocument(CosmosDocumentMapper.to_document(transaction)),
                change_event_create(event),
            ]
            inventory = after_by_item[transaction.item_id]
            outcome[transaction.operation_id] = {
                "transaction_id": transaction.transaction_id,
                "item_id": transaction.item_id,
                "quantity_added": transaction.quantity_delta,
                "inventory_quantity": inventory.quantity,
                "unit": inventory.unit,
            }
        shared = [
            ReplaceDocument(
                CosmosDocumentMapper.to_document(update.record),
                if_match_etag=update.expected_etag,
            )
            for update in plan.inventory_updates
        ]
        return DomainWritePlan(
            operations_by_id=operations_by_id,
            shared_operations=shared,
            outcome=outcome,
        )

    async def _purchase_input(
        self, record: ApprovalRecord, operation: StoredOperation
    ) -> PurchaseInput:
        if operation.action != COMPLETE_PURCHASE:
            raise UnsupportedAction(f"No writes for action {operation.action!r} yet.")
        values = operation.values
        shopping, shopping_etag = _unchanged(
            operation,
            await self._reader.read_shopping_item_with_etag(
                household_id=record.household_id,
                shopping_item_id=str(values["shopping_item_id"]),
            ),
        )
        inventory, inventory_etag = _unchanged(
            operation,
            await self._reader.read_inventory_item_with_etag(
                household_id=record.household_id,
                item_id=str(values["item_id"]),
            ),
        )
        return PurchaseInput(
            operation_id=operation.operation_id,
            transaction_id=str(values["transaction_id"]),
            shopping=shopping,
            shopping_etag=shopping_etag,
            inventory=inventory,
            inventory_etag=inventory_etag,
            purchased_quantity=_quantity(values["purchased_quantity"]),
        )


def _unchanged[RecordT: (ShoppingItem, InventoryItem)](
    operation: StoredOperation, current: MappedDocument[RecordT] | None
) -> tuple[RecordT, str]:
    """The record and its ETag, if it is still the version on the card (PR #47 §4 point 6).

    A deleted or missing record counts as changed.
    """
    if current is None or current.etag is None:
        raise TargetChanged(f"A record on {operation.operation_id} is gone.")
    shown = {target.record_id: target.etag for target in operation.targets}
    if current.document_id not in shown:
        raise ValueError(f"The card has no version for {current.document_id}.")
    if shown[current.document_id] != current.etag:
        raise TargetChanged(current.document_id)
    return current.record, current.etag


def _quantity(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("The card's quantity must be a number.")
    return float(value)
