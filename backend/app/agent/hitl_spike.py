"""A1 HITL spike: pause before a write, resume after approval (Day 2 experiment).

Scope of this spike:
- Minimal LangGraph flow: propose -> approve (interrupt) -> execute.
- Rule-based planner only ("我買了 2 瓶牛奶"); no Azure OpenAI yet.
- In-memory checkpoints; paused state is lost on restart.
- Not wired to HTTP routes; /agent/messages and /agent/resume still return 501.

Safety rules followed (AGENTS.md "HITL rules"):
- Pause BEFORE any persistent mutation.
- Resume carries only approval_id + approved; values come from server-side state.
- Re-sending the same approval never executes the write twice.
- A failed write can be retried by approving again; the retry reuses the same
  operation_id, and cancelling after a failure writes nothing.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.models.agent import (
    AgentRequest,
    AgentResponse,
    AgentResult,
    ApprovalPresentation,
    PendingApproval,
    ProposedOperation,
    ResultKind,
    ResumeRequest,
)

_UNITS = "瓶|罐|包|盒|個|條|支|袋|顆|片|卷|件"
_PURCHASE = re.compile(rf"買了\s*(\d+)\s*({_UNITS})?\s*(\S.*)")


class WriteExecutor(Protocol):
    """Performs the approved write. Step 4 supplies a fake; B1 supplies the real one."""

    async def __call__(self, household_id: str, operation: ProposedOperation) -> None: ...


class ApprovalNotFoundError(LookupError):
    """Unknown approval_id, or it belongs to another household."""


class _State(TypedDict, total=False):
    household_id: str
    approval_id: str
    message: str
    operation: dict[str, Any]
    approved: bool
    executed: bool


@dataclass
class _ApprovalRecord:
    household_id: str
    operation: ProposedOperation
    response: AgentResponse | None = None  # set once resolved; reused on re-send
    failed: bool = False  # last attempt raised; next approval retries


def _plan(message: str) -> dict[str, Any] | None:
    """Rule-based stand-in for the LLM: turn a purchase sentence into one operation."""
    match = _PURCHASE.search(message)
    if not match:
        return None
    quantity, unit, name = int(match.group(1)), match.group(2) or "", match.group(3).strip()
    return {
        "operation_id": uuid.uuid4().hex,
        "description": f"新增 {quantity} {unit}{name} 到庫存",
        "values": {"name": name, "quantity": quantity, "unit": unit},
    }


class HitlSpikeRuntime:
    """Implements the provisional AgentRuntime port (respond / resume)."""

    def __init__(self, executor: WriteExecutor) -> None:
        self._executor = executor
        self._approvals: dict[str, _ApprovalRecord] = {}
        self._lock = asyncio.Lock()
        self._graph = self._build_graph()

    def _build_graph(self):
        async def propose(state: _State) -> _State:
            # No side effects here: only resolve what would be written.
            return {"operation": _plan(state["message"])}

        async def approve(state: _State) -> _State:
            # Pause. Execution resumes here with the user's decision.
            decision = interrupt({"approval_id": state["approval_id"]})
            return {"approved": bool(decision)}

        async def execute(state: _State) -> _State:
            if not state.get("approved"):
                return {"executed": False}
            operation = ProposedOperation(**state["operation"])
            await self._executor(state["household_id"], operation)
            return {"executed": True}

        def after_propose(state: _State) -> str:
            return "approve" if state.get("operation") else END

        builder = StateGraph(_State)
        builder.add_node("propose", propose)
        builder.add_node("approve", approve)
        builder.add_node("execute", execute)
        builder.add_edge(START, "propose")
        builder.add_conditional_edges("propose", after_propose, ["approve", END])
        builder.add_edge("approve", "execute")
        builder.add_edge("execute", END)
        return builder.compile(checkpointer=InMemorySaver())

    @staticmethod
    def _config(approval_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": approval_id}}

    async def respond(self, request: AgentRequest) -> AgentResponse:
        approval_id = uuid.uuid4().hex
        config = self._config(approval_id)
        await self._graph.ainvoke(
            {
                "household_id": request.household_id,
                "approval_id": approval_id,
                "message": request.message,
            },
            config,
        )
        snapshot = await self._graph.aget_state(config)
        if not snapshot.next:  # finished without pausing: nothing to write
            return AgentResponse(message="（實驗）目前只處理「我買了 2 瓶牛奶」這類句子。")

        operation = ProposedOperation(**snapshot.values["operation"])
        self._approvals[approval_id] = _ApprovalRecord(request.household_id, operation)
        return AgentResponse(
            message=f"要{operation.description}嗎？",
            pending_approval=PendingApproval(
                approval_id=approval_id,
                presentation=ApprovalPresentation.SINGLE_ACTION,
                operations=[operation],
            ),
        )

    async def resume(self, request: ResumeRequest) -> AgentResponse:
        async with self._lock:
            record = self._approvals.get(request.approval_id)
            if record is None or record.household_id != request.household_id:
                raise ApprovalNotFoundError(request.approval_id)
            if record.response is not None:  # already resolved: never write twice
                return record.response

            operation = record.operation
            if record.failed and not request.approved:  # cancel after a failure
                return self._finish(record, executed=False)

            config = self._config(request.approval_id)
            try:
                if record.failed:
                    # Retry: continue from the failed step; same operation_id.
                    await self._graph.ainvoke(None, config)
                else:
                    await self._graph.ainvoke(Command(resume=request.approved), config)
            except Exception:
                record.failed = True
                message = "寫入失敗，請再按一次確認。"
                return AgentResponse(
                    message=message,
                    results=[
                        AgentResult(
                            kind=ResultKind.ACTION_RESULT,
                            summary=message,
                            data={
                                "operation_id": operation.operation_id,
                                "executed": False,
                                "retryable": True,
                            },
                        )
                    ],
                    pending_approval=PendingApproval(
                        approval_id=request.approval_id,
                        presentation=ApprovalPresentation.SINGLE_ACTION,
                        operations=[operation],
                    ),
                )

            snapshot = await self._graph.aget_state(config)
            return self._finish(record, executed=bool(snapshot.values.get("executed")))

    @staticmethod
    def _finish(record: _ApprovalRecord, executed: bool) -> AgentResponse:
        operation = record.operation
        if executed:
            message = f"已{operation.description}。"
        else:
            message = "已取消，沒有寫入任何資料。"
        record.response = AgentResponse(
            message=message,
            results=[
                AgentResult(
                    kind=ResultKind.ACTION_RESULT,
                    summary=message,
                    data={"operation_id": operation.operation_id, "executed": executed},
                )
            ],
        )
        return record.response
