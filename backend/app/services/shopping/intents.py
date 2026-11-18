"""C1 Shopping Intent creation over the shared durable write repository.

The public API and request DTO are intentionally outside this module. A trusted
caller supplies a stable request identity and canonical values when retrying a
logical request; this C1 service resolves durable operation and item IDs from it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Protocol

from app.models.domain import ShoppingItem, ShoppingStatus
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_write import CreateDocument, OperationIdConflict, WriteStatus
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


class ShoppingIntentReader(Protocol):
    async def read_shopping_item(
        self, *, household_id: str, shopping_item_id: str
    ) -> ShoppingItem | None: ...


@dataclass(frozen=True)
class AddShoppingIntent:
    """Backend command with stable request identity across retries.

    The request_id is not a public DTO promise yet. This C1 proposal scopes it to
    household and add-operation kind to derive durable operation/item identities.
    The backend owns creation timestamps and reuses the first stored timestamp
    when retrying the same request.
    """

    household_id: str
    request_id: str
    name: str
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

    def __init__(
        self,
        writer: ShoppingIntentWriter,
        reader: ShoppingIntentReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._writer = writer
        self._reader = reader
        self._clock = clock or (lambda: datetime.now(UTC))

    async def add(self, command: AddShoppingIntent) -> ShoppingIntentWriteResult:
        name = command.name.strip()
        if not command.household_id or not command.household_id.strip():
            raise ShoppingIntentError("household_id must not be empty.")
        # The identity resolver trims the scope. Use that same canonical scope
        # for the domain record, receipt lookup, and batch.
        household_id = command.household_id.strip()
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

        operation_id, shopping_item_id = resolve_shopping_intent_identity(
            household_id=household_id,
            request_id=command.request_id,
        )

        existing = await self._reader.read_shopping_item(
            household_id=household_id,
            shopping_item_id=shopping_item_id,
        )
        created_at = (
            existing.created_at
            if self._matches_command(
                existing, household_id=household_id, name=name, command=command
            )
            else self._clock()
        )
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ShoppingIntentError("Backend clock must return a timezone-aware timestamp.")

        item = self._build_item(
            command=command,
            household_id=household_id,
            shopping_item_id=shopping_item_id,
            name=name,
            created_at=created_at,
        )
        try:
            status = await self._write(
                household_id=household_id,
                operation_id=operation_id,
                item=item,
            )
        except OperationIdConflict:
            existing = await self._reader.read_shopping_item(
                household_id=household_id,
                shopping_item_id=shopping_item_id,
            )
            if not self._matches_command(
                existing, household_id=household_id, name=name, command=command
            ):
                raise
            item = self._build_item(
                command=command,
                household_id=household_id,
                shopping_item_id=shopping_item_id,
                name=name,
                created_at=existing.created_at,
            )
            status = await self._write(
                household_id=household_id,
                operation_id=operation_id,
                item=item,
            )

        return ShoppingIntentWriteResult(item=item, status=status)

    @staticmethod
    def _matches_command(
        item: ShoppingItem | None,
        *,
        household_id: str,
        name: str,
        command: AddShoppingIntent,
    ) -> bool:
        return (
            item is not None
            and item.household_id == household_id
            and item.name == name
            and item.quantity == command.quantity
            and item.unit == (command.unit.strip() if command.unit is not None else None)
        )

    @staticmethod
    def _build_item(
        *,
        command: AddShoppingIntent,
        household_id: str,
        shopping_item_id: str,
        name: str,
        created_at: datetime,
    ) -> ShoppingItem:
        return ShoppingItem(
            shopping_item_id=shopping_item_id,
            household_id=household_id,
            name=name,
            quantity=command.quantity,
            unit=command.unit.strip() if command.unit is not None else None,
            status=ShoppingStatus.PENDING,
            created_at=created_at,
            updated_at=created_at,
        )

    async def _write(
        self,
        *,
        household_id: str,
        operation_id: str,
        item: ShoppingItem,
    ) -> WriteStatus:
        return await self._writer.execute_idempotent_batches(
            household_id=household_id,
            operations_by_id={
                operation_id: (CreateDocument(CosmosDocumentMapper.to_document(item)),),
            },
        )
