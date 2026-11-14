"""C1 Shopping Intent creation over the shared durable write repository.

The public API and request DTO are intentionally outside this module. A trusted
caller supplies a stable request identity and canonical values when retrying a
logical request; this C1 service resolves durable operation and item IDs from it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from app.models.domain import ShoppingItem, ShoppingStatus
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_write import CreateDocument, WriteStatus
from app.services.shopping.identity import resolve_shopping_intent_identity


class ShoppingIntentError(ValueError):
    """The Shopping Intent command is invalid or cannot be safely applied."""


class ShoppingIntentWriter(Protocol):
    """Narrow C1 dependency on the durable, operation-idempotent write port."""

    async def execute_idempotent_batches(
        self,
        *,
        household_id: str,
        operations_by_id: Mapping[str, Sequence[CreateDocument]],
    ) -> WriteStatus: ...


@dataclass(frozen=True)
class AddShoppingIntent:
    """Backend command; request_id and time must be stable across retries.

    The request_id is not a public DTO promise yet. This C1 proposal scopes it to
    household and add-operation kind to derive durable operation/item identities.
    """

    household_id: str
    request_id: str
    name: str
    created_at: datetime
    quantity: int | None = None
    unit: str | None = None


@dataclass(frozen=True)
class ShoppingIntentWriteResult:
    item: ShoppingItem
    status: WriteStatus


@dataclass(frozen=True)
class ShoppingIntentCreatedEvent:
    """C1 proposal for append-only provenance of a direct Shopping addition.

    The record type and fields need B1 approval before this service is composed
    into a public write path. This event is distinct from purchase ChangeEvent,
    which requires Inventory and Transaction identities that a direct add lacks.
    """

    event_id: str
    household_id: str
    operation_id: str
    shopping_item_id: str
    occurred_at: datetime

    def to_document(self) -> dict[str, object]:
        occurred_at = (
            self.occurred_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        return {
            "id": f"shopping_intent_event:{self.event_id}",
            "record_type": "shopping_intent_event",
            "event_id": self.event_id,
            "household_id": self.household_id,
            "operation_id": self.operation_id,
            "shopping_item_id": self.shopping_item_id,
            "occurred_at": occurred_at,
        }


class ShoppingIntentService:
    """Validate and durably add one pending Shopping Intent.

    A repeat with the same operation ID and identical canonical document returns
    ``ALREADY_APPLIED`` from the repository. Reusing the operation ID with changed
    values is rejected by that repository's receipt fingerprint check.
    """

    def __init__(self, writer: ShoppingIntentWriter) -> None:
        self._writer = writer

    async def add(self, command: AddShoppingIntent) -> ShoppingIntentWriteResult:
        household_id = command.household_id
        name = command.name.strip()
        if not household_id or not household_id.strip():
            raise ShoppingIntentError("household_id must not be empty.")
        if not command.request_id or not command.request_id.strip():
            raise ShoppingIntentError("request_id must not be empty.")
        if not name:
            raise ShoppingIntentError("name must not be empty.")
        if command.quantity is not None and (
            isinstance(command.quantity, bool)
            or not isinstance(command.quantity, int)
            or command.quantity <= 0
        ):
            raise ShoppingIntentError("planned quantity must be a positive integer when supplied.")
        if command.unit is not None and not command.unit.strip():
            raise ShoppingIntentError("unit must not be empty when supplied.")
        if command.created_at.tzinfo is None or command.created_at.utcoffset() is None:
            raise ShoppingIntentError("created_at must include a timezone.")

        operation_id, shopping_item_id = resolve_shopping_intent_identity(
            household_id=household_id,
            request_id=command.request_id,
        )

        item = ShoppingItem(
            shopping_item_id=shopping_item_id,
            household_id=household_id,
            name=name,
            quantity=command.quantity,
            unit=command.unit.strip() if command.unit is not None else None,
            status=ShoppingStatus.PENDING,
            created_at=command.created_at,
            updated_at=command.created_at,
        )
        document = CosmosDocumentMapper.to_document(item)
        event = ShoppingIntentCreatedEvent(
            event_id=operation_id,
            household_id=household_id,
            operation_id=operation_id,
            shopping_item_id=shopping_item_id,
            occurred_at=command.created_at,
        )
        status = await self._writer.execute_idempotent_batches(
            household_id=household_id,
            operations_by_id={
                operation_id: (
                    CreateDocument(document),
                    CreateDocument(event.to_document()),
                ),
            },
        )
        return ShoppingIntentWriteResult(item=item, status=status)
