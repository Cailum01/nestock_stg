"""PROVISIONAL: A1/C2 must agree on the wire contract before real integration.

These are presentation envelopes, never proof that a mutation is authorized.
Generic operation values must not be dispatched directly to domain services.
"""

from enum import StrEnum

from pydantic import Field, JsonValue

from app.models.base import ContractModel


class ApprovalPresentation(StrEnum):
    SINGLE_ACTION = "single_action"
    BATCH_ACTION = "batch_action"
    PURCHASE_OPTIONS = "purchase_options"


class ResultKind(StrEnum):
    INVENTORY = "inventory"
    VISION = "vision"
    FORECAST = "forecast"
    PURCHASE_OPTIONS = "purchase_options"
    ACTION_RESULT = "action_result"


class AgentRequest(ContractModel):
    household_id: str
    message: str = Field(min_length=1)
    image_ref: str | None = Field(default=None, min_length=1)
    """Optional opaque reference from POST /api/media/upload-authorization (A2).

    The Agent verifies the referenced media belongs to household_id before A2's
    recognition reads it (open-decisions D-5); an unknown or foreign reference is
    refused. Recognition results are display-only and write nothing.
    """


class AgentResult(ContractModel):
    kind: ResultKind
    summary: str
    data: dict[str, JsonValue] = Field(default_factory=dict)


class ProposedOperation(ContractModel):
    """Resolved values for display; operation identity survives retries."""

    operation_id: str
    description: str
    values: dict[str, JsonValue]


class PendingApproval(ContractModel):
    approval_id: str
    presentation: ApprovalPresentation
    operations: list[ProposedOperation] = Field(min_length=1)


class AgentResponse(ContractModel):
    message: str
    results: list[AgentResult] = Field(default_factory=list)
    pending_approval: PendingApproval | None = None


class ResumeRequest(ContractModel):
    """Server must load and validate exact approved values; no client write payload."""

    household_id: str
    approval_id: str
    approved: bool
