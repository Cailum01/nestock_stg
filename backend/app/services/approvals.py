"""A1 confirmation-card ("approval") service.

Implements the rules in `contracts/approval-lifecycle.md` (PR #47, Issue #48
option B): one card per approval, `pending -> confirmed -> completed`, no stored
`failed` status, expiry decided from `expires_at`, and recovery by reading the
card and its operation receipts before any retry.

`POST /agent/resume` calls this service when Cosmos is configured. The only domain
write builder so far is A1's purchase adapter over C1's plan
(`services/purchase_writes.py`, Issue #60).

The Cosmos batch limit is checked when a card is saved, before it can be shown
(A1's answer in Issue #60): a card whose largest approval would not fit one batch
is never stored. The check runs again at confirmation, because records can change
in between.

Storage goes through `ApprovalStoragePort`, which uses the same method names and
arguments as B1's `CosmosWriteRepository` (PR #51, #54; `list_open_approvals` in
PR #61, with its query client wired in PR #72), so that repository fits without an
adapter. `tests/test_approvals_cosmos_write.py` runs this service on that repository.

What each approved operation writes (inventory, transaction, shopping status,
alias) belongs to the domain owners (C1, B1) through `DomainWriteBuilder`; this
service only decides when a write may happen and how to recover safely.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol

from pydantic import Field, JsonValue

from app.models.agent import (
    ApprovalPresentation,
    PendingApproval,
    ProposedOperation,
    ResumeRequest,
)
from app.models.base import ContractModel
from app.services.cosmos_write import (
    BatchOperation,
    BatchOperationConflict,
    OperationIdConflict,
    ReplaceDocument,
    WriteStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_LIFETIME = timedelta(minutes=30)
"""Card lifetime (#39). Configurable through `ApprovalService(lifetime=...)`."""

CLEANUP_WINDOW = timedelta(hours=24)
"""TTL window for cards that can no longer be used; same as orphaned media (#30)."""

KEEP = -1
"""Cosmos `ttl` value meaning "never expire" (the container default TTL must be on)."""

MAX_BATCH_OPERATIONS = 100
"""Cosmos transactional batch limit (PR #47 section 5)."""

RECORD_TYPE = "pending_approval"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CancelReason(StrEnum):
    USER = "user"
    """The user pressed "cancel"."""
    REPLACED = "replaced"
    """The Agent replaced the card with a new one (PR #47 sections 6 and 7)."""


class TargetVersion(ContractModel):
    """A record the operation will change, and its version when the card was made."""

    record_id: str = Field(min_length=1)
    etag: str = Field(min_length=1)


class StoredOperation(ContractModel):
    operation_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    """Domain action the builder executes, for example `complete_purchase`."""
    description: str
    values: dict[str, JsonValue]
    """The exact resolved values shown to the user."""
    targets: list[TargetVersion] = Field(default_factory=list)


class StoredOption(ContractModel):
    option_id: str = Field(min_length=1)
    values: dict[str, JsonValue]


class ApprovalRecord(ContractModel):
    """Internal card record (PR #47 section 2). Not a public API model."""

    approval_id: str
    household_id: str
    presentation: ApprovalPresentation
    operations: list[StoredOperation] = Field(min_length=1)
    options: list[StoredOption] = Field(default_factory=list)
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    completed_at: datetime | None = None
    approved_operation_ids: list[str] | None = None
    selected_option_id: str | None = None
    outcome: dict[str, JsonValue] | None = None
    cancel_reason: CancelReason | None = None

    @property
    def operation_ids(self) -> list[str]:
        return [operation.operation_id for operation in self.operations]

    def is_past_expiry(self, now: datetime) -> bool:
        return now >= self.expires_at

    def can_retry(self, now: datetime) -> bool:
        """A confirmed card can be retried; a purchase-options card only before expiry."""
        if self.status is not ApprovalStatus.CONFIRMED:
            return False
        if self.presentation is ApprovalPresentation.PURCHASE_OPTIONS:
            return not self.is_past_expiry(now)
        return True

    def ttl_seconds(self, lifetime: timedelta) -> int:
        """Cosmos `ttl` by status (A1 decision, PR #47 section 3).

        Cosmos counts TTL from the record's last write.
        """
        if self.status is ApprovalStatus.PENDING:
            return int((lifetime + CLEANUP_WINDOW).total_seconds())
        if self.status in (ApprovalStatus.CONFIRMED, ApprovalStatus.COMPLETED):
            return KEEP
        return int(CLEANUP_WINDOW.total_seconds())

    def to_document(self, lifetime: timedelta) -> dict[str, object]:
        document: dict[str, object] = self.model_dump(mode="json")
        document["id"] = approval_document_id(self.approval_id)
        document["record_type"] = RECORD_TYPE
        document["ttl"] = self.ttl_seconds(lifetime)
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> ApprovalRecord:
        fields = {
            key: value
            for key, value in document.items()
            if not key.startswith("_") and key not in ("id", "record_type", "ttl")
        }
        return cls.model_validate(fields)


def approval_document_id(approval_id: str) -> str:
    """Same Cosmos id format as B1's `read_pending_approval` in PR #51."""
    return f"{RECORD_TYPE}:{approval_id}"


@dataclass(frozen=True)
class NewOperation:
    """A resolved operation the Agent wants the user to approve."""

    action: str
    description: str
    values: Mapping[str, JsonValue]
    targets: Sequence[TargetVersion] = ()


@dataclass(frozen=True)
class NewOption:
    option_id: str
    values: Mapping[str, JsonValue]


@dataclass(frozen=True)
class DomainWritePlan:
    """What the approved operations write, built by the domain owners (C1, B1)."""

    operations_by_id: Mapping[str, Sequence[BatchOperation]]
    shared_operations: Sequence[BatchOperation] = ()
    """Writes shared by several operations, such as one merged inventory update when two
    approved items add to the same inventory record (B1's mapping in Issue #60)."""
    outcome: Mapping[str, JsonValue] = field(default_factory=dict)
    """Result of each operation, stored on the card and returned on retries."""


class StoredApproval(Protocol):
    """Same shape as `PendingApprovalRecord` in PR #51."""

    @property
    def document(self) -> Mapping[str, object]: ...

    @property
    def etag(self) -> str: ...


class ApprovalStoragePort(Protocol):
    """Storage the service needs from B1, all scoped to one household partition."""

    # Same names and arguments as CosmosWriteRepository (PR #51).
    async def read_pending_approval(
        self, *, household_id: str, approval_id: str
    ) -> StoredApproval | None: ...

    async def read_operation_receipt(
        self, *, household_id: str, operation_id: str
    ) -> object | None: ...

    async def execute_idempotent_batches(
        self,
        *,
        household_id: str,
        operations_by_id: Mapping[str, Sequence[BatchOperation]],
        shared_operations: Sequence[BatchOperation] = (),
    ) -> WriteStatus: ...

    # Same names and arguments as CosmosWriteRepository in PR #54.
    async def create_pending_approval(
        self, *, household_id: str, approval: Mapping[str, object], ttl_seconds: int
    ) -> StoredApproval: ...

    async def replace_pending_approval(
        self,
        *,
        household_id: str,
        approval_id: str,
        document: Mapping[str, object],
        if_match_etag: str,
    ) -> StoredApproval:
        """Approval-only conditional write (never `completed`); returns the new record.

        Raises `BatchOperationConflict` when the ETag no longer matches.
        """
        ...

    # Same name and arguments as CosmosWriteRepository in PR #61.
    async def list_open_approvals(self, *, household_id: str) -> Sequence[StoredApproval]:
        """Cards whose stored status is `pending` or `confirmed`."""
        ...


class DomainWriteBuilder(Protocol):
    """Turns approved operations into batch writes (C1, B1).

    The builder reads the current records and raises `TargetChanged` when a target's
    ETag differs from the version stored on the card (PR #47 section 4 point 6).
    Replace operations must use those stored ETags.
    """

    async def build(self, record: ApprovalRecord) -> DomainWritePlan: ...


class ApprovalNotFound(LookupError):
    """Unknown `approval_id`, or it belongs to another household."""


class InvalidSelection(ValueError):
    """The resume request does not match the card type or the stored selection."""


class BatchTooLarge(ValueError):
    """The approved operations do not fit in one Cosmos transactional batch."""


class TargetChanged(Exception):
    """A record on the card changed after the card was made."""


class ResumeOutcome(StrEnum):
    COMPLETED = "completed"
    """Written now."""
    ALREADY_COMPLETED = "already_completed"
    """Written earlier; the stored outcome is returned."""
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    """Past `expires_at`; the Agent asks the user again."""
    NEEDS_NEW_CARD = "needs_new_card"
    """A record changed; the old card is cancelled and the Agent makes a new one."""
    NOT_WRITTEN = "not_written"
    """The write did not happen or its result is unknown; retry with the same card."""
    INCONSISTENT = "inconsistent"
    """Some operation receipts exist without completion; stop and report (#27)."""


@dataclass(frozen=True)
class ResumeResult:
    outcome: ResumeOutcome
    record: ApprovalRecord | None = None

    @property
    def written(self) -> bool:
        """True only when the change is in the database; say "done" only then (#52)."""
        return self.outcome in (ResumeOutcome.COMPLETED, ResumeOutcome.ALREADY_COMPLETED)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class ApprovalService:
    """Prepares and saves cards, and carries out confirm, cancel, retry and recovery (PR #47)."""

    def __init__(
        self,
        storage: ApprovalStoragePort,
        writes: DomainWriteBuilder,
        *,
        lifetime: timedelta = DEFAULT_LIFETIME,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_id,
    ) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("lifetime must be positive.")
        self._storage = storage
        self._writes = writes
        self._lifetime = lifetime
        self._clock = clock
        self._new_id = id_factory

    # ----- creating and showing cards -------------------------------------------------

    def prepare(
        self,
        *,
        household_id: str,
        presentation: ApprovalPresentation,
        operations: Sequence[NewOperation],
        options: Sequence[NewOption] = (),
    ) -> ApprovalRecord:
        """Build a new `pending` card without storing it. IDs are made here, never by the App.

        IDs and times are fixed once, so a retry of `save` sends exactly the same card
        (PR #47 section 6, rule 6: retries reuse the same identity).
        """
        if not household_id:
            raise ValueError("household_id must not be empty.")
        _check_card_shape(presentation, operations, options)
        now = self._clock()
        return ApprovalRecord(
            approval_id=self._new_id(),
            household_id=household_id,
            presentation=presentation,
            operations=[
                StoredOperation(
                    operation_id=self._new_id(),
                    action=operation.action,
                    description=operation.description,
                    values=dict(operation.values),
                    targets=list(operation.targets),
                )
                for operation in operations
            ],
            options=[
                StoredOption(option_id=option.option_id, values=dict(option.values))
                for option in options
            ],
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now + self._lifetime,
        )

    async def save(self, record: ApprovalRecord) -> ApprovalRecord:
        """Store a card from `prepare`. Safe to call again with the same card.

        Before storing, the writes are planned as if every operation were approved (on a
        purchase-options card, once per option). If that would exceed the Cosmos batch
        limit, `BatchTooLarge` is raised and the card is not stored, so the user never
        sees it; the Agent has to spread the items over several cards (Issue #60).
        `TargetChanged` from this check means a record changed after the Agent read it;
        the card is not stored either.

        B1's `create_pending_approval` (PR #54) returns the stored card when the same
        approval_id already exists with identical content, and refuses different
        content. So retrying after a lost response never adds a second card.
        """
        if record.status is not ApprovalStatus.PENDING:
            raise ValueError("Only a pending card can be saved.")
        await self._check_batch_limit(record)
        stored = await self._storage.create_pending_approval(
            household_id=record.household_id,
            approval=record.to_document(self._lifetime),
            ttl_seconds=record.ttl_seconds(self._lifetime),
        )
        return ApprovalRecord.from_document(stored.document)

    async def _check_batch_limit(self, record: ApprovalRecord) -> None:
        """Raise `BatchTooLarge` if the largest possible approval would not fit one batch.

        Approving every operation is the largest selection; on a purchase-options card
        each option is tried, because options may write different amounts. `decided_at`
        is not known yet, so `created_at` stands in for it: the time changes values on
        the writes, not how many writes there are.
        """
        option_ids: list[str | None] = [option.option_id for option in record.options]
        for option_id in option_ids or [None]:
            trial = record.model_copy(
                update={
                    "approved_operation_ids": record.operation_ids,
                    "selected_option_id": option_id,
                    "decided_at": record.created_at,
                }
            )
            _check_plan_size(trial, await self._writes.build(trial))

    @staticmethod
    def to_pending_approval(record: ApprovalRecord) -> PendingApproval:
        """Public card for the App. Purchase options sit on the card, as in v8 section 12."""
        # The card's own option_id comes last, so the App always sends back an ID on this card.
        options = [{**o.values, "option_id": o.option_id} for o in record.options]
        operations = [
            ProposedOperation(
                operation_id=operation.operation_id,
                action=operation.action,
                description=operation.description,
                values=dict(operation.values),
            )
            for operation in record.operations
        ]
        return PendingApproval(
            approval_id=record.approval_id,
            presentation=record.presentation,
            operations=operations,
            options=options,
        )

    async def list_open(self, *, household_id: str) -> list[ApprovalRecord]:
        """Cards the Agent mentions on the next message (PR #47 section 8).

        Expired cards are left out and recorded as `expired` here.
        """
        now = self._clock()
        open_cards: list[ApprovalRecord] = []
        for stored in await self._storage.list_open_approvals(household_id=household_id):
            record = ApprovalRecord.from_document(stored.document)
            if record.status is ApprovalStatus.PENDING and not record.is_past_expiry(now):
                open_cards.append(record)
            elif record.can_retry(now):
                open_cards.append(record)
            elif record.status in (ApprovalStatus.PENDING, ApprovalStatus.CONFIRMED):
                await self._record_expired(record, stored.etag)
        return open_cards

    # ----- confirm, cancel, retry -----------------------------------------------------

    async def resume(self, request: ResumeRequest) -> ResumeResult:
        """Handle the App's confirm or cancel for one card."""
        stored = await self._load(request.household_id, request.approval_id)
        record = ApprovalRecord.from_document(stored.document)
        if not request.approved:
            return await self._cancel(record, stored.etag, CancelReason.USER)
        return await self._approve(record, stored.etag, request)

    async def cancel_replaced(self, *, household_id: str, approval_id: str) -> ResumeResult:
        """Cancel a card the Agent replaced with a new one (PR #47 section 7)."""
        stored = await self._load(household_id, approval_id)
        record = ApprovalRecord.from_document(stored.document)
        return await self._cancel(record, stored.etag, CancelReason.REPLACED)

    async def _approve(
        self, record: ApprovalRecord, etag: str, request: ResumeRequest
    ) -> ResumeResult:
        now = self._clock()
        finished = _finished_result(record)
        if finished is not None:
            return finished

        if record.status is ApprovalStatus.PENDING:
            if record.is_past_expiry(now):
                return await self._record_expired(record, etag)
            approved_ids, option_id = _selection_for(record, request)
            # decided_at is fixed before planning, so the builder can use it (for example as
            # the change event's recorded_at, Issue #60) and a retry plans with the same value.
            selected = record.model_copy(
                update={
                    "approved_operation_ids": approved_ids,
                    "selected_option_id": option_id,
                    "decided_at": now,
                }
            )
            plan = await self._plan(selected, etag)
            if isinstance(plan, ResumeResult):
                return plan
            confirmed = selected.model_copy(update={"status": ApprovalStatus.CONFIRMED})
            try:
                stored_confirmed = await self._storage.replace_pending_approval(
                    household_id=record.household_id,
                    approval_id=record.approval_id,
                    document=confirmed.to_document(self._lifetime),
                    if_match_etag=etag,
                )
            except BatchOperationConflict:
                return await self._after_lost_race(record, request)
            except Exception:
                logger.warning("Confirm write for %s failed; reading back.", record.approval_id)
                return await self._recover(record)
            return await self._write(confirmed, stored_confirmed.etag, plan)

        # CONFIRMED: retry with the stored selection.
        if not record.can_retry(now):
            return await self._record_expired(record, etag)
        _check_retry_selection(record, request)
        if await self._has_receipts(record):
            # Re-read: another request's batch may have just committed (then it is done).
            return await self._recover(record)
        plan = await self._plan(record, etag)
        if isinstance(plan, ResumeResult):
            return plan
        return await self._write(record, etag, plan)

    async def _plan(self, record: ApprovalRecord, etag: str) -> DomainWritePlan | ResumeResult:
        """Build the writes and check the batch limit again before anything is written.

        `save` already checked the limit; records may have changed since then.
        """
        try:
            plan = await self._writes.build(record)
        except TargetChanged:
            return await self._replace_after_change(record, etag)
        _check_plan_size(record, plan)
        return plan

    async def _write(
        self, confirmed: ApprovalRecord, etag: str, plan: DomainWritePlan
    ) -> ResumeResult:
        """One batch: `confirmed -> completed`, domain writes and receipts (PR #47 §5)."""
        completed = confirmed.model_copy(
            update={
                "status": ApprovalStatus.COMPLETED,
                "completed_at": self._clock(),
                "outcome": dict(plan.outcome),
            }
        )
        receipts = len(plan.operations_by_id)
        domain_writes = sum(len(ops) for ops in plan.operations_by_id.values())
        # PR #51 orders a batch as: receipts, per-operation writes, shared writes.
        # The completed card goes last, after the plan's own shared writes.
        approval_index = receipts + domain_writes + len(plan.shared_operations)
        try:
            status = await self._storage.execute_idempotent_batches(
                household_id=confirmed.household_id,
                operations_by_id=plan.operations_by_id,
                shared_operations=(
                    *plan.shared_operations,
                    ReplaceDocument(completed.to_document(self._lifetime), if_match_etag=etag),
                ),
            )
        except BatchOperationConflict as conflict:
            if conflict.operation_index == approval_index:
                return await self._recover(confirmed)
            return await self._replace_after_change(confirmed, None)
        except OperationIdConflict:
            # Same operation_id with different values is a conflict, not a retry (§6).
            return await self._replace_after_change(confirmed, None)
        except Exception:
            logger.warning("Batch for %s failed or timed out; reading back.", confirmed.approval_id)
            return await self._recover(confirmed)
        if status is WriteStatus.ALREADY_APPLIED:
            return await self._recover(confirmed)
        return ResumeResult(ResumeOutcome.COMPLETED, completed)

    async def _cancel(
        self, record: ApprovalRecord, etag: str, reason: CancelReason, *, attempts: int = 2
    ) -> ResumeResult:
        """`pending` or `confirmed` -> `cancelled`; a completed write is never undone."""
        now = self._clock()
        finished = _finished_result(record)
        if finished is not None:
            return finished
        if reason is CancelReason.USER and not (
            (record.status is ApprovalStatus.PENDING and not record.is_past_expiry(now))
            or record.can_retry(now)
        ):
            return await self._record_expired(record, etag)
        update: dict[str, object] = {"status": ApprovalStatus.CANCELLED, "cancel_reason": reason}
        if reason is CancelReason.USER:
            update["decided_at"] = now
        cancelled = record.model_copy(update=update)
        try:
            await self._storage.replace_pending_approval(
                household_id=record.household_id,
                approval_id=record.approval_id,
                document=cancelled.to_document(self._lifetime),
                if_match_etag=etag,
            )
        except BatchOperationConflict:
            # Another request changed the card first: a batch committed (keep it), or a
            # confirm moved it to `confirmed` (still cancellable, so try once more).
            stored = await self._load(record.household_id, record.approval_id)
            current = ApprovalRecord.from_document(stored.document)
            finished = _finished_result(current)
            if finished is not None or attempts <= 1:
                return finished or ResumeResult(ResumeOutcome.NOT_WRITTEN, current)
            return await self._cancel(current, stored.etag, reason, attempts=attempts - 1)
        return ResumeResult(ResumeOutcome.CANCELLED, cancelled)

    async def _replace_after_change(self, record: ApprovalRecord, etag: str | None) -> ResumeResult:
        """Nothing is written; the old card becomes `cancelled` (A1 decision)."""
        if etag is None:
            stored = await self._load(record.household_id, record.approval_id)
            record = ApprovalRecord.from_document(stored.document)
            etag = stored.etag
            finished = _finished_result(record)
            if finished is not None:
                return finished
        result = await self._cancel(record, etag, CancelReason.REPLACED)
        if result.outcome is ResumeOutcome.CANCELLED:
            return ResumeResult(ResumeOutcome.NEEDS_NEW_CARD, result.record)
        return result

    async def _after_lost_race(
        self, record: ApprovalRecord, request: ResumeRequest
    ) -> ResumeResult:
        """Two confirms arrived together; the other one changed the card first."""
        stored = await self._load(record.household_id, record.approval_id)
        current = ApprovalRecord.from_document(stored.document)
        if current.status is ApprovalStatus.CONFIRMED:
            return await self._approve(current, stored.etag, request)
        return _finished_result(current) or ResumeResult(ResumeOutcome.NOT_WRITTEN, current)

    async def _recover(self, record: ApprovalRecord) -> ResumeResult:
        """Read the card and its receipts, then decide (PR #47 section 6)."""
        stored = await self._load(record.household_id, record.approval_id)
        current = ApprovalRecord.from_document(stored.document)
        finished = _finished_result(current)
        if finished is not None:
            return finished
        if current.status is ApprovalStatus.CONFIRMED and await self._has_receipts(current):
            return ResumeResult(ResumeOutcome.INCONSISTENT, current)
        return ResumeResult(ResumeOutcome.NOT_WRITTEN, current)

    async def _has_receipts(self, record: ApprovalRecord) -> bool:
        for operation_id in record.approved_operation_ids or ():
            receipt = await self._storage.read_operation_receipt(
                household_id=record.household_id, operation_id=operation_id
            )
            if receipt is not None:
                return True
        return False

    async def _record_expired(self, record: ApprovalRecord, etag: str) -> ResumeResult:
        """Expiry is decided from `expires_at`; recording it is best effort (§3)."""
        expired = record.model_copy(update={"status": ApprovalStatus.EXPIRED})
        try:
            await self._storage.replace_pending_approval(
                household_id=record.household_id,
                approval_id=record.approval_id,
                document=expired.to_document(self._lifetime),
                if_match_etag=etag,
            )
        except Exception:
            logger.info(
                "Could not record %s as expired; still treated as expired.", record.approval_id
            )
        return ResumeResult(ResumeOutcome.EXPIRED, expired)

    async def _load(self, household_id: str, approval_id: str) -> StoredApproval:
        stored = await self._storage.read_pending_approval(
            household_id=household_id, approval_id=approval_id
        )
        if stored is None:
            raise ApprovalNotFound("No such approval in this household.")
        return stored


def _check_plan_size(record: ApprovalRecord, plan: DomainWritePlan) -> None:
    """The plan covers exactly the approved operations and fits one Cosmos batch."""
    if set(plan.operations_by_id) != set(record.approved_operation_ids or ()):
        raise ValueError("The write plan must cover exactly the approved operations.")
    size = (
        len(plan.operations_by_id)  # one operation receipt per operation
        + sum(len(ops) for ops in plan.operations_by_id.values())
        + len(plan.shared_operations)
        + 1  # the completed card is one more write in the same batch
    )
    if size > MAX_BATCH_OPERATIONS:
        raise BatchTooLarge(
            f"{size} operations exceed the Cosmos batch limit of {MAX_BATCH_OPERATIONS}."
        )


def _finished_result(record: ApprovalRecord) -> ResumeResult | None:
    if record.status is ApprovalStatus.COMPLETED:
        return ResumeResult(ResumeOutcome.ALREADY_COMPLETED, record)
    if record.status is ApprovalStatus.CANCELLED:
        return ResumeResult(ResumeOutcome.CANCELLED, record)
    if record.status is ApprovalStatus.EXPIRED:
        return ResumeResult(ResumeOutcome.EXPIRED, record)
    return None


def _check_card_shape(
    presentation: ApprovalPresentation,
    operations: Sequence[NewOperation],
    options: Sequence[NewOption],
) -> None:
    """The three card types (v8 section 12).

    The item_match card (Issue #90) has an agreed format but no confirm rules yet,
    so it is refused here.
    """
    if presentation is ApprovalPresentation.ITEM_MATCH:
        raise ValueError("Item-match cards are not supported yet.")
    if not operations:
        raise ValueError("A card needs at least one operation.")
    if presentation is ApprovalPresentation.PURCHASE_OPTIONS:
        if len(operations) != 1:
            raise ValueError("A purchase-options card has exactly one operation.")
        option_ids = [option.option_id for option in options]
        if not option_ids or any(not option_id for option_id in option_ids):
            raise ValueError("A purchase-options card needs options with IDs.")
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("Option IDs must be unique.")
        if any("option_id" in option.values for option in options):
            raise ValueError("Option values must not contain option_id; the card sets it.")
        return
    if options:
        raise ValueError("Only a purchase-options card has options.")
    if presentation is ApprovalPresentation.SINGLE_ACTION and len(operations) != 1:
        raise ValueError("A single-action card has exactly one operation.")


def _selection_for(record: ApprovalRecord, request: ResumeRequest) -> tuple[list[str], str | None]:
    """Validate a first confirmation against the card type (v8 section 12, §4 point 5)."""
    ids = request.approved_operation_ids
    option_id = request.selected_option_id
    if record.presentation is ApprovalPresentation.SINGLE_ACTION:
        if ids is not None or option_id is not None:
            raise InvalidSelection("A single-action card takes no selection.")
        return record.operation_ids, None
    if record.presentation is ApprovalPresentation.BATCH_ACTION:
        if option_id is not None:
            raise InvalidSelection("A batch card takes approved_operation_ids only.")
        if not ids:
            raise InvalidSelection("Approve at least one operation.")
        unknown = set(ids) - set(record.operation_ids)
        if unknown:
            raise InvalidSelection("approved_operation_ids must be on this card.")
        return [op for op in record.operation_ids if op in set(ids)], None
    if ids is not None:
        raise InvalidSelection("A purchase-options card takes selected_option_id only.")
    if option_id is None:
        raise InvalidSelection("Choose one purchase option.")
    if option_id not in {option.option_id for option in record.options}:
        raise InvalidSelection("selected_option_id must be on this card.")
    return record.operation_ids, option_id


def _check_retry_selection(record: ApprovalRecord, request: ResumeRequest) -> None:
    """On a retry the stored selection is used; a different one is rejected (§4)."""
    if record.presentation is ApprovalPresentation.SINGLE_ACTION:
        ok = request.approved_operation_ids is None and request.selected_option_id is None
    elif record.presentation is ApprovalPresentation.BATCH_ACTION:
        ok = request.selected_option_id is None and set(
            request.approved_operation_ids or ()
        ) == set(record.approved_operation_ids or ())
    else:
        ok = (
            request.approved_operation_ids is None
            and request.selected_option_id == record.selected_option_id
        )
    if not ok:
        raise InvalidSelection("The selection was fixed at the first confirmation.")
