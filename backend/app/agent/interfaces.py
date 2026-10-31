from typing import Protocol

from app.models.agent import AgentRequest, AgentResponse, ResumeRequest


class AgentRuntime(Protocol):
    """Provisional application port; graph, state and checkpoints remain A1-owned."""

    async def respond(self, request: AgentRequest) -> AgentResponse: ...

    async def resume(self, request: ResumeRequest) -> AgentResponse: ...
