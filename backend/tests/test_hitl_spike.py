"""A1 HITL spike tests (Day 2): pause before a write, resume after approval.

Every test uses its own runtime and fake executor, and runs its whole scenario
inside one asyncio.run() call (the skeleton has no pytest-asyncio).
"""

import asyncio

import pytest

from app.agent.fakes import FakeInventoryService, FakeWriteExecutor
from app.agent.hitl_spike import ApprovalNotFoundError, HitlSpikeRuntime
from app.models.agent import (
    AgentRequest,
    AgentResponse,
    ApprovalPresentation,
    ProposedOperation,
    ResumeRequest,
)

HOUSEHOLD = "demo-household"
OTHER_HOUSEHOLD = "another-household"
MESSAGE = "我買了 2 瓶牛奶"


def _new() -> tuple[HitlSpikeRuntime, FakeWriteExecutor]:
    executor = FakeWriteExecutor()
    return HitlSpikeRuntime(executor, FakeInventoryService()), executor


async def _propose(runtime: HitlSpikeRuntime) -> AgentResponse:
    response = await runtime.respond(AgentRequest(household_id=HOUSEHOLD, message=MESSAGE))
    assert response.pending_approval is not None
    return response


def _resume(approval_id: str, approved: bool, household_id: str = HOUSEHOLD) -> ResumeRequest:
    return ResumeRequest(household_id=household_id, approval_id=approval_id, approved=approved)


def _executed(response: AgentResponse) -> bool:
    return bool(response.results[0].data["executed"])


def test_approve_writes_once():
    async def scenario():
        runtime, executor = _new()
        proposed = await _propose(runtime)
        pending = proposed.pending_approval
        assert pending.presentation == ApprovalPresentation.SINGLE_ACTION
        assert pending.operations[0].values == {
            "item_id": "fake-milk",
            "name": "牛奶",
            "quantity": 2,
            "unit": "瓶",
        }
        assert executor.call_count == 0  # paused BEFORE any write

        done = await runtime.resume(_resume(pending.approval_id, approved=True))
        assert _executed(done)
        assert done.pending_approval is None
        assert executor.call_count == 1
        assert executor.write_count == 1

    asyncio.run(scenario())


def test_reject_writes_nothing():
    async def scenario():
        runtime, executor = _new()
        pending = (await _propose(runtime)).pending_approval

        done = await runtime.resume(_resume(pending.approval_id, approved=False))
        assert not _executed(done)
        assert executor.call_count == 0
        assert executor.write_count == 0

    asyncio.run(scenario())


def test_duplicate_resume_is_idempotent():
    async def scenario():
        runtime, executor = _new()
        pending = (await _propose(runtime)).pending_approval

        first = await runtime.resume(_resume(pending.approval_id, approved=True))
        second = await runtime.resume(_resume(pending.approval_id, approved=True))
        assert second == first  # replayed result, no second execution
        assert executor.call_count == 1
        assert executor.write_count == 1

    asyncio.run(scenario())


def test_invalid_or_foreign_approval_rejected():
    async def scenario():
        runtime, executor = _new()
        pending = (await _propose(runtime)).pending_approval

        with pytest.raises(ApprovalNotFoundError):  # unknown approval id
            await runtime.resume(_resume("not-a-real-id", approved=True))
        with pytest.raises(ApprovalNotFoundError):  # another household's approval
            await runtime.resume(
                _resume(pending.approval_id, approved=True, household_id=OTHER_HOUSEHOLD)
            )
        assert executor.call_count == 0
        assert executor.write_count == 0

    asyncio.run(scenario())


def test_unknown_message_no_pause():
    async def scenario():
        runtime, executor = _new()
        response = await runtime.respond(
            AgentRequest(household_id=HOUSEHOLD, message="今天天氣好嗎")
        )
        assert response.pending_approval is None
        assert executor.call_count == 0

    asyncio.run(scenario())


def test_failure_then_retry_writes_once():
    async def scenario():
        runtime, executor = _new()
        pending = (await _propose(runtime)).pending_approval
        operation_id = pending.operations[0].operation_id
        executor.fail_next()

        failed = await runtime.resume(_resume(pending.approval_id, approved=True))
        assert "請再按一次" in failed.message
        assert failed.results[0].data["retryable"] is True
        assert failed.pending_approval is not None  # confirm card shown again
        assert failed.pending_approval.approval_id == pending.approval_id
        assert executor.write_count == 0

        done = await runtime.resume(_resume(pending.approval_id, approved=True))
        assert _executed(done)
        assert executor.call_count == 2
        assert executor.write_count == 1
        assert [call.operation_id for call in executor.calls] == [operation_id, operation_id]

    asyncio.run(scenario())


def test_failure_then_cancel_writes_nothing():
    async def scenario():
        runtime, executor = _new()
        pending = (await _propose(runtime)).pending_approval
        executor.fail_next()

        await runtime.resume(_resume(pending.approval_id, approved=True))
        done = await runtime.resume(_resume(pending.approval_id, approved=False))
        assert not _executed(done)
        assert executor.call_count == 1
        assert executor.write_count == 0

    asyncio.run(scenario())


def test_fake_executor_dedupes_by_operation():
    async def scenario():
        executor = FakeWriteExecutor()
        operation = ProposedOperation(
            operation_id="op-1",
            action="add_stock",
            description="新增 2 瓶牛奶 到庫存",
            values={"name": "牛奶", "quantity": 2, "unit": "瓶"},
        )
        await executor(HOUSEHOLD, operation)
        await executor(HOUSEHOLD, operation)
        assert executor.call_count == 2
        assert executor.write_count == 1

    asyncio.run(scenario())


async def _ask(runtime: HitlSpikeRuntime, message: str) -> AgentResponse:
    return await runtime.respond(AgentRequest(household_id=HOUSEHOLD, message=message))


def test_unit_comes_from_inventory_when_sentence_has_none():
    async def scenario():
        runtime, executor = _new()
        response = await _ask(runtime, "我買了 2 牛奶")
        assert response.pending_approval is not None
        assert response.pending_approval.operations[0].values["unit"] == "瓶"
        assert executor.call_count == 0

    asyncio.run(scenario())


def test_alias_uses_item_name_and_unit():
    async def scenario():
        runtime, _ = _new()
        response = await _ask(runtime, "我買了 3 米")
        values = response.pending_approval.operations[0].values
        assert values["item_id"] == "fake-rice"
        assert values["name"] == "白米"
        assert values["unit"] == "包"

    asyncio.run(scenario())


def test_different_unit_asks_instead_of_card():
    async def scenario():
        runtime, executor = _new()
        response = await _ask(runtime, "我買了 1 箱牛奶")
        assert response.pending_approval is None  # no card until converted
        assert "瓶" in response.message and "1 箱" in response.message
        assert executor.call_count == 0

    asyncio.run(scenario())


def test_item_not_in_inventory_asks_for_unit():
    async def scenario():
        runtime, executor = _new()
        response = await _ask(runtime, "我買了 2 包薯片")
        assert response.pending_approval is None
        assert "薯片" in response.message and "單位" in response.message
        assert executor.call_count == 0

    asyncio.run(scenario())
