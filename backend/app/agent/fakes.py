"""TEST AND DEMO ONLY. Never use in production.

FakeWriteExecutor stands in for B1's real write while A1 validates pause/resume.
It never touches the skeleton demo fixtures; it only records into its own lists.

Two separate counters so the two safety layers can be tested independently:
- calls:  every time the Agent invoked the write (tests the Agent layer).
- writes: unique (household_id, operation_id) actually stored; a repeated
          operation_id is ignored, which is what the real database must also do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.agent import ProposedOperation
from app.models.domain import InventoryItem


class FakeWriteError(RuntimeError):
    """Simulated write failure (network/database error)."""


@dataclass(frozen=True)
class FakeCall:
    household_id: str
    operation_id: str


class FakeWriteExecutor:
    """Implements the WriteExecutor port: (household_id, operation) -> None."""

    def __init__(self) -> None:
        self.calls: list[FakeCall] = []
        self.writes: dict[tuple[str, str], dict] = {}
        self._fail_times = 0

    def fail_next(self, times: int = 1) -> None:
        """Make the next `times` calls raise FakeWriteError before writing."""
        self._fail_times += times

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def write_count(self) -> int:
        return len(self.writes)

    async def __call__(self, household_id: str, operation: ProposedOperation) -> None:
        self.calls.append(FakeCall(household_id, operation.operation_id))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise FakeWriteError("simulated write failure")
        key = (household_id, operation.operation_id)
        if key in self.writes:  # duplicate operation identity: ignore
            return
        self.writes[key] = dict(operation.values)


_FAKE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class FakeInventoryService:
    """Implements the InventoryService port with two fixed items per household.

    Gives the HITL spike a system-defined unit to use: 牛奶 is counted in 瓶,
    白米 in 包.
    """

    async def list_items(self, household_id: str) -> list[InventoryItem]:
        return [
            InventoryItem(
                item_id="fake-milk",
                household_id=household_id,
                name="牛奶",
                quantity=1,
                unit="瓶",
                created_at=_FAKE_TIME,
                updated_at=_FAKE_TIME,
            ),
            InventoryItem(
                item_id="fake-rice",
                household_id=household_id,
                name="白米",
                quantity=2,
                unit="包",
                aliases=["米"],
                created_at=_FAKE_TIME,
                updated_at=_FAKE_TIME,
            ),
        ]
