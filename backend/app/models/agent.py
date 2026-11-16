"""PROVISIONAL: A1/C2 must agree on the wire contract before real integration.

These are presentation envelopes, never proof that a mutation is authorized.
Generic operation values must not be dispatched directly to domain services.
"""

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import Field, JsonValue, field_validator, model_validator

from app.models.base import ContractModel


class ApprovalPresentation(StrEnum):
    SINGLE_ACTION = "single_action"
    BATCH_ACTION = "batch_action"
    PURCHASE_OPTIONS = "purchase_options"
    ITEM_MATCH = "item_match"
    """"Is this the household item you meant?" (Issue #90): one operation and the
    options `same_item` and `new_item`. Not issued yet: the approval service rejects
    it until its confirm rules are built."""


class ResultKind(StrEnum):
    INVENTORY = "inventory"
    VISION = "vision"
    FORECAST = "forecast"
    PURCHASE_OPTIONS = "purchase_options"
    ACTION_RESULT = "action_result"


class CardChange(ContractModel):
    """One line changed on the card with 送出修改 (Issue #162): only the changed fields.

    A field left out stays as it is on the card. `purchased_on` is a Taipei date;
    an explicit null sets the line back to today (no purchase day on the card).
    """

    operation_id: str = Field(min_length=1)
    quantity: int | None = Field(default=None, ge=1, strict=True)
    purchased_on: date | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if "quantity" in self.model_fields_set and self.quantity is None:
            raise ValueError("quantity cannot be null; leave it out to keep it.")
        if not {"quantity", "purchased_on"} & self.model_fields_set:
            raise ValueError("Each change needs quantity or purchased_on.")
        return self


class AgentRequest(ContractModel):
    household_id: str
    message: str = Field(min_length=1)
    image_ref: str | None = Field(default=None, min_length=1)
    """Optional opaque reference from POST /api/media/upload-authorization (A2).

    The Agent verifies the referenced media belongs to household_id before A2's
    recognition reads it (open-decisions D-5); an unknown or foreign reference is
    refused. Recognition results are display-only and write nothing.
    """
    replaces_approval_id: str | None = Field(default=None, min_length=1)
    """Set only when the user taps 送出修改 on a pending card (Issue #91).

    `message` then describes only the changed fields, and the old card is replaced
    by a new one (contracts/approval-lifecycle.md section 7). Ordinary chat leaves
    it out. The old card must be in the same household and still pending; it is
    cancelled only after the new card is stored.
    """
    card_changes: list[CardChange] | None = Field(default=None, min_length=1)
    """送出修改 as data (Issue #162): the changed lines of the `replaces_approval_id` card.

    With it, the backend makes the new card from the stored card and these changes
    without the model; `message` is only the chat text. Without it, `message`
    describes the change for the model as before (Issue #91).
    """

    @model_validator(mode="after")
    def _changes_need_the_card(self) -> Self:
        if self.card_changes is not None and self.replaces_approval_id is None:
            raise ValueError("card_changes needs replaces_approval_id.")
        return self


class AgentResult(ContractModel):
    kind: ResultKind
    summary: str
    data: dict[str, JsonValue] = Field(default_factory=dict)


class ProposedOperation(ContractModel):
    """Resolved values for display; operation identity survives retries."""

    operation_id: str
    action: str = Field(min_length=1)
    """What the operation does, so the App can choose how to show it (Issue #90).

    Agreed with C2: `confirm_item_match` (item_match card) and `create_item` (a new
    inventory item). Other names are provisional until their cards are built.
    """
    description: str
    values: dict[str, JsonValue]


class PendingApproval(ContractModel):
    approval_id: str
    presentation: ApprovalPresentation
    operations: list[ProposedOperation] = Field(min_length=1)
    options: list[dict[str, JsonValue]] = Field(default_factory=list)
    """purchase_options: each option is its `option_id` plus the values shown
    (store, price, url, source, ...), as in v8 section 12.
    item_match: exactly `same_item` and `new_item`, each with a `label` (Issue #90).
    Empty for other cards."""


class AgentResponse(ContractModel):
    message: str
    results: list[AgentResult] = Field(default_factory=list)
    pending_approval: PendingApproval | None = None


class ResumeRequest(ContractModel):
    """Server must load and validate exact approved values; no client write payload.

    One envelope for every approval presentation (v8 section 12, D-10, Issue #90):
    - single_action: no selection field.
    - batch_action: approved_operation_ids lists the displayed operations the
      user kept; operations left out are not written.
    - purchase_options: selected_option_id is the one option the user chose.
    - item_match (Issue #90): selected_option_id is `same_item` (add to the matched
      item and remember the user's word as its alias) or `new_item` (write nothing;
      the reply carries a card that creates a new item).

    When approved is false nothing is written and both selection fields are
    ignored. Which field a presentation requires, and whether the IDs belong to
    the stored approval, must be checked against the server-side approval, not
    here.
    """

    household_id: str
    approval_id: str
    approved: bool
    approved_operation_ids: list[str] | None = Field(default=None, min_length=1)
    selected_option_id: str | None = Field(default=None, min_length=1)

    @field_validator("approved_operation_ids")
    @classmethod
    def _operation_ids_are_distinct(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if any(not operation_id.strip() for operation_id in value):
            raise ValueError("approved_operation_ids must not contain empty IDs")
        if len(set(value)) != len(value):
            raise ValueError("approved_operation_ids must not repeat an ID")
        return value

    @field_validator("selected_option_id")
    @classmethod
    def _option_id_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("selected_option_id must not be blank")
        return value
