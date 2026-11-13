"""C1 Shopping Intent creation over the shared durable write repository.

The public API and the mapping from client request identity to operation identity
are intentionally outside this module. A trusted caller must supply the same
resolved identities and canonical values when retrying a logical request.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.models.domain import ShoppingItem, ShoppingStatus
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_write import CreateDocument, WriteStatus


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
    """Resolved backend command; IDs and time must be stable across retries.

    ``operation_id`` is the durable write identity. The caller is responsible for
    binding its request identity to this value and replaying this command unchanged.
    C1 does not define a public request DTO or choose how client request IDs map to
    operation IDs.
    """

    household_id: str
    operation_id: str
    shopping_item_id: str
    name: str
    created_at: datetime
    quantity: int | None = None
    unit: str | None = None


@dataclass(frozen=True)
class ShoppingIntentWriteResult:
    item: ShoppingItem
    status: WriteStatus


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
        operation_id = command.operation_id
        shopping_item_id = command.shopping_item_id
        name = command.name.strip()
        if not household_id or not household_id.strip():
            raise ShoppingIntentError("household_id must not be empty.")
        if not operation_id or not operation_id.strip():
            raise ShoppingIntentError("operation_id must not be empty.")
        if not shopping_item_id or not shopping_item_id.strip():
            raise ShoppingIntentError("shopping_item_id must not be empty.")
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
        status = await self._writer.execute_idempotent_batches(
            household_id=household_id,
            operations_by_id={
                operation_id: (CreateDocument(document),),
            },
        )
        return ShoppingIntentWriteResult(item=item, status=status)
