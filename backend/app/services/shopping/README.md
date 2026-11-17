# C1 ShoppingService design and approval write builder

This module implements C1's Shopping purchase/removal rules, pure domain planning,
and approval batch assembly. Normal Shopping reads use the shared `ShoppingService`
port. Writes that span Shopping and other domains go through the approval workflow:
the planner and `ShoppingWriteBuilder` do not authorize or execute mutations;
they build the exact household batch that A1's `ApprovalService` commits through
B1's `CosmosWriteRepository`.

## C1 decisions and implementation boundary

- Shopping is a reminder of which items to buy; a planned quantity is not needed
  to create or complete an intent. `ShoppingItem.quantity` and `unit` remain
  optional legacy fields for compatibility. Purchase previews do not validate,
  return, default, or use the legacy quantity as a limit. Completion preserves any
  stored legacy values. Only the actual quantity explicitly supplied and approved
  for a purchase increments Inventory and becomes the purchase Transaction delta.
- One approval's selected writes are one household-scoped atomic batch. Each logical
  write keeps its stored operation identity across retries. Cancellation performs
  no Shopping, Inventory, Transaction, event, or receipt business write.
- Removing a pending Shopping entry is a soft delete (`deleted_at`). It leaves the
  public status and Inventory unchanged and creates no purchase Transaction/event.
- A new Inventory target requires confirmed name, actual quantity, unit, and
  location (location may be absent only when the user confirmed that absence).
  C1 accepts the stable `item_id` produced by B1's
  `resolve_inventory_item_id(operation_id)`; C1 and the App do not define its
  encoding. `ShoppingPlanService` checks that the supplied ID matches the B1
  resolver for that operation identity. Creation uses a create operation in the
  same approval batch, never a fabricated zero-quantity snapshot or ETag.
- Validation and planning are all-or-nothing. A failed member rejects the group;
  no partial plan is returned. A fresh card is required when a target, quantity,
  unit, location, or other approved value changes.

The C1 planner and builder, including new-target creates, are implemented and
covered by offline integration tests. A1's stored-card resolver supports
Shopping-backed `create_item` lines using B1's operation-derived stable ID; the
Agent/resume integration has offline coverage for mixed existing/new targets,
confirmation, retry and cancellation. This is backend test evidence, not live
Azure model or Android App end-to-end acceptance. Issue #90's item-match/alias
journey remains outside this completed new-target purchase path.

The production composition now injects A1's `CardInputResolver` into
`ShoppingWriteBuilder`. It rehydrates only the fixed, server-stored card lines and
their ETags into C1 purchase inputs; client-provided values are never decoded as a
write. With `NESTOCK_COSMOS_*` configured, the Agent can create a supported purchase
card and `POST /agent/resume` confirms or cancels that stored card. Confirmation
writes the C1 plan as one household batch; cancellation writes no domain changes.
Without Cosmos configuration, no approval service or purchase-card tool is enabled,
and `/agent/resume` returns 503 without saving changes. Plans are not approval.

## Domain semantics

### Direct Shopping additions (#52)

Shopping represents purchase intent. A Direct App addition needs a name; legacy
quantity and unit are optional and must not be fabricated. Direct App and Agent
additions must use the shared C1 ShoppingService, with backend validation and a
success response only after persistence succeeds. The App's explicit Add action is
the confirmation for this direct Shopping write; Agent initiated additions remain
behind the Agent approval flow.

Issue #52 did not freeze a public endpoint or request/response DTO. Before enabling
the Direct App write path, C1, A1 and B1 must agree how a stable request identity
binds to the operation ID and durable receipt across retries and lost responses;
C1 and C2 must then align the DTO. Do not treat the local Shopping presentation
prototype or the purchase flow's approval receipt as that contract. Cosmos
document mapping, partition key, receipt schema and ID rules remain authoritative
in [`datalake/azure-data-platform-design.md`](../../../../datalake/azure-data-platform-design.md)
and its linked B1 decisions; this C1 document describes only the service handoff.

`ShoppingIntentService.add(AddShoppingIntent)` now implements the C1 domain write:
it validates a named pending intent, permits omitted legacy quantity and unit,
builds the authoritative `ShoppingItem` document, and calls B1's
`execute_idempotent_batches`. This C1 proposal derives stable operation and item
IDs from the household plus a caller-supplied request ID. Retrying the same request
with the same payload returns `already_applied`; changing values under that request
identity is rejected by the durable receipt fingerprint. The request-ID contract
and item-ID format remain subject to B1/A1/C2 review. This is an internal C1
service seam; it is not yet composed into a public route or Agent tool.

For atomic provenance, C1 proposes creating a `shopping_intent_event` in the same
operation batch as the new Shopping row and B1 operation receipt. Its proposed
identity is the operation ID, with household, Shopping item ID and event time.
This is separate from the purchase `ChangeEvent`, which requires Inventory and
Transaction IDs that a direct Shopping addition does not have. The new event
record type is provisional pending B1 review; the service must not be composed into
a public write path until the record shape and mapper support are approved.

Offline verification record (2026-11-14): C1 added stable request identity mapping
and a same-batch `shopping_intent_event` proposal for direct Shopping additions,
plus a plan-time check that a new
Inventory ID equals B1's stable resolver output for the operation, with rejection
coverage for caller-chosen IDs. Thirteen focused tests for
`ShoppingIntentService.add()` pass, covering item creation and normalization,
same-identity retry, changed-payload receipt rejection, and validation failures
before writing. Full backend validation passed: Ruff check, format check, and
pytest (751 passed, 30 deselected). The focused tests use a fake Cosmos batch
client and do not write to live Cosmos or alter the 31-record `demo-household`
baseline; they verify the internal service only, not the public HTTP or App flow.

Issue #180 clarifies that Shopping quantity is legacy data, not the meaning of a
new Shopping intent. A purchase completes the selected intent regardless of its
legacy quantity. Any stored legacy value is preserved for compatibility, while
only the explicitly approved actual quantity updates Inventory and the purchase
Transaction. No remaining pending intent is inferred. Completed entries disappear
from the active list without hard deletion.

Per [Issue #63](https://github.com/nestock-app/nestock/issues/63), actual purchase
counts use integer arithmetic. Shopping's legacy quantity/unit can be absent; no
value of one or default unit is invented. Actual purchase quantity must be explicit
and positive. Fractional, nonfinite, boolean and string counts fail. The current Pydantic models use integer
counts; no floating-point compatibility path is part of the contract.
An actual unit must be resolved and match the Inventory target and any planned unit.
There is no unit conversion or automatic purchase beyond an existing plan.

Manual removal only marks a valid pending Shopping entry with storage-only
`deleted_at`, preserving the public pending/completed status. It never updates
Inventory, creates a purchase Transaction, or reverses earlier effects.

## Plans and new targets (#90)

### Standalone stock additions (#168)

#### Photo-card adapter boundary (#173)

B1's `PhotoItemWriteBuilder` remains the adapter for approved no-Shopping
photo cards: it owns that card flow's stored-card validation and write routing.
It calls C1's pure `ShoppingPlanService.plan_adjustment` for the domain
adjustment rules. C1's `ShoppingWriteBuilder` continues to assemble the writes
for its own supported approval workflows. These adapters keep their own
card-specific orchestration and persistence mapping; #173 does not require a
shared write builder or a refactor of PR #171. This boundary does not enable
photo-card generation or presentation.

`AdjustmentInput` and `plan_adjustment` cover any confirmed new-item stock
addition without a Shopping row, including `add_stock` and photo-created items.
The stored card supplies the exact name, positive actual quantity, unit and
location (including a confirmed absent location). C1 records incoming stock as
one `adjustment` Transaction with a positive `quantity_delta`; this describes an
initial confirmed quantity and does not change an existing Inventory item. The
matching ChangeEvent uses `event_id = operation_id`, links the approval,
transaction and new `item_id`, leaves `shopping_item_id` null, and uses the
stored approval `decided_at` for both `occurred_at` and `recorded_at`.

`ShoppingWriteBuilder` already sends the Inventory create, Transaction and event
through one operation batch. The caller must resolve inputs from the stored
approval; the builder checks every value against that card and checks that the
stable operation-derived item ID is absent in the household. It neither creates
a Shopping row nor acts as the approval or persistence layer. Uncertain matches
must be resolved before C1 receives an adjustment input.

`ShoppingPreviewService` provides display-only previews for existing targets and
removal. `ShoppingPlanService.plan_purchase` accepts selected `PurchaseInput`s:

- Existing target: original Inventory snapshot and ETag; a missing Shopping link
  needs explicit `resolved_inventory_item_id`, which cannot override an existing link.
- New target: `new_inventory=NewInventoryTarget(item_id, name, unit, location)`,
  `inventory=None`, `inventory_etag=None`. All values must come from the confirmed
  server proposal. The stable item_id comes from B1's backend resolver using the
  operation identity, never from Flutter. C1 does not choose an ID encoding.

A new target produces `PurchasePlan.inventory_creates`, distinct from conditional
`inventory_updates`. It never constructs an existing zero-stock snapshot or fake
ETag. Linked Shopping entries cannot silently create a different target. Duplicate
new IDs and create/update collisions reject the whole group. Existing shared
Inventory increments are combined against one consistent snapshot and ETag.

The plan contains Shopping completion, actual Transactions, combined Inventory
replacements and optional Inventory creates. Inputs are preserved, validation is
all-or-nothing, and repeated stored inputs produce identical IDs and values. Pure
planning cannot prove absence, freshness, authorization or durable idempotency.

A1 resolves ambiguous existing-item matches before handing inputs to C1. The
caller obtains a new item's stable ID from B1's resolver and
binds it to the stored operation; the C1 planner validates and carries that exact
ID through the create, Transaction, event, receipt, and outcome. C1 does not choose
or rank match candidates; A1 owns that resolution. Standalone gifts / `add_stock`
without a Shopping entry need A1's card lifecycle and B1's event scope agreement;
no dummy Shopping ID or event field is invented here.

For #90/#175, C1's `ShoppingWriteBuilder` can now plan the `same_item` outcome of
one stored `item_match` card as a normal purchase, provided A1's resolver supplies
the exact existing Inventory and pending Shopping snapshots, their ETags, the
displayed quantity/unit, and a stored `transaction_id`. C1 validates those values
against the card, then uses the normal purchase batch and does not add an alias.
The `new_item` choice is not a purchase write: A1 closes the match card without a
domain write, asks for the item's location, then issues a separate new-item card.
Runtime item-match cards remain unavailable until A1 enables their shape and
selection lifecycle and updates `CardInputResolver`; this C1 builder change alone
is not end-to-end support.

## Time contract (#60)

A1 confirmed the split and fixed cutoff in
[Issue #60](https://github.com/nestock-app/nestock/issues/60#issuecomment-6703640304):

| Value | Meaning |
| --- | --- |
| `recorded_at` | First server-stored `decided_at`; all domain updated_at and new-item created_at |
| `PurchaseInput.purchased_at` | Optional shown/resolved event time; Transaction.created_at and event occurred_at |

Without purchased_at, the Transaction uses decided_at. Both timestamps must be
aware; purchased_at cannot exceed decided_at. Retries reuse both times and all
identities. Historical purchases do not backdate domain updates or reconstruct
past stock. A1 owns natural-language date/timezone resolution and showing dates;
changed dates require a new card. B1 confirmed event-time queries and no production
history migration; B2 still owns forecast enablement.

Issue #113 closed with the event-time decision above. `recorded_at` is a planner
argument, not a Transaction field;
ChangeEvent records both occurred_at and recorded_at. A1's pending #71 helper
resolves dates, #104 passes stored purchased_at through, and #96's current card
producer does not supply a historical purchased_at. Helper/resolver support is
not evidence that historical-date entry is wired through the Agent/App.

## Batch builder (#27 / #60)

`ShoppingWriteBuilder(reader, resolver)` implements A1's `DomainWriteBuilder` port.
Its `build(ApprovalRecord)` returns `DomainWritePlan`, without executing a write.
`reader` uses B1's household-scoped point-read shape. The injected A1
`ShoppingInputResolver.resolve(record)` returns `ResolvedShoppingChanges` with
`purchases`, `RemovalInput`s and/or no-Shopping `AdjustmentInput`s. This is an
internal typed handoff, not a final public wire schema. It supports single-action
and batch-action cards only.

A1's `CardInputResolver` validates the supported stored card action and binds its
actual quantity, unit, linked target, transaction ID and optional purchase time to
the exact server-stored proposal. It accepts only the card's fixed selected
operation IDs; it never uses resume-edited values, creates new retry IDs or performs
fresh matching. The pending save-time trial uses created_at temporarily for
count-only planning; it authorizes nothing.

C1 additionally verifies the exact selected ID set, unique Shopping targets,
card-bound source ETags, household, current source snapshots and absence of a new
target. A photo `create_item` adjustment must match the stored card's action,
transaction ID, name, quantity, unit and location; its new item ID must be the B1
operation resolver output and absent from the household partition. No fake
Shopping row or Shopping ID is introduced. Deleted targets stop. Resolver ordering
is canonicalized to card order.
All source replacements preserve storage-only metadata while stripping Cosmos
system properties. The final ETag/create conditions close the race after reads.

B1's mapping is used unchanged:

- Per operation: Shopping conditional replacement, Transaction create and
  `change_event_create`; a new Inventory create is in its originating operation.
- Photo adjustment: Inventory create, `adjustment` Transaction create and a
  `ChangeEvent` with `shopping_item_id = null` are grouped under the same
  operation ID. `event_id` equals `operation_id`; Transaction and ChangeEvent use
  the card's stored `decided_at`. B1's repository adds the operation receipt and
  A1 appends the completed approval in that same household batch.
- Shared: one conditional Inventory replacement per distinct existing target.
- Removal: conditional Shopping tombstone only; no Inventory, Transaction or
  purchase event is manufactured.
- B1 adds one receipt per operation. A1 appends approval confirmed -> completed
  in the same household batch. Full count includes all of these, max 100.

Purchase outcomes now include `quantity_added`, final combined
`inventory_quantity` and `unit`, in addition to stable record IDs. Two selected
operations for the same Inventory item both report its final combined quantity;
a new target reports its created quantity. Removal has no quantity/unit outcome.
These fields support PR #96's response and are stored on the approval for retries.
The existing-target acceptance fixture now uses PR #95's card values (`name`,
`purchased_quantity`, `purchased_unit`, `shopping_item_id`, `item_id`,
`transaction_id`), rather than a separate `quantity` test field.

The builder rejects an oversized group before any persistent write; it never
splits an approved group. A1 must split large intents into separate cards before
showing them. Replacements always use the card's original ETags; new records use
create, never upsert. An ID collision rejects the batch without overwriting.

A1's lifecycle fixes selection on first confirmation and handles expiry/cancel,
lost responses and stored outcomes. Completed retries return the stored outcome;
confirmed with no receipts may retry with original versions. Partial receipts or
known receipt/domain inconsistencies require reconciliation, never automatic
repair or fresh IDs. C1 does not audit arbitrary later edits to completed history.

## Verification and remaining integration

Run from backend:

```sh
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check app/services/shopping tests/test_shopping_planning.py tests/test_shopping_preview.py tests/test_shopping_workflow.py
```

`test_shopping_workflow.py` connects the real C1 planner/builder, A1 ApprovalService
and B1 CosmosWriteRepository. Only the Cosmos container and A1 input resolver are
faked. Cases include shared targets, new targets, removal, partial selection,
cancel, household rejection, stale versions, create conflicts, atomic rollback,
timeouts before/after commit, stable retries, partial receipts and exact 100-action
acceptance / oversized save rejection. These are offline integration tests, not
Agent/App end-to-end or live Cosmos evidence.

The name-only Pydantic case exercises the current optional Shopping quantity/unit
model. The tests use B1's stable ID resolver for new-target fixtures and do not
redefine its format. See the linked Issues and pull requests for current integration
evidence and owner work.

## Final Demo core purchase acceptance contract

For the Final Demo, C1 has one core purchase journey. It is the only purchase
journey that C1 treats as required acceptance; the additional journeys listed in
the Demo readiness checklist must stay Deferred unless the owners explicitly mark
them Included before the script is frozen.

```text
baseline
→ 洗衣精買回 2 瓶
→ approval
→ confirm
→ Inventory 1 → 3
→ Shopping completed (the legacy quantity value remains 2)
→ exactly one new purchase Transaction for this operation
→ retry the same approval: no duplicate effect
→ cancel a separate approval: no business write
→ reset: the complete baseline is restored
```

The baseline is one `demo-household`: `seed-detergent` is confirmed Inventory
quantity 1, and pending `seed-shopping-detergent` is linked to that same item ID
with legacy quantity 2 and unit `瓶`. The approval must use the stored card and
its fixed operation identity; confirmation must add exactly 2 confirmed bottles,
complete that linked Shopping row, and preserve its legacy quantity value. “Exactly one
Transaction” means exactly one new effective `purchase` Transaction associated with
the confirmed operation, not that the baseline has no historical Transactions.

Before confirmation, the Inventory quantity remains 1. A repeat of the completed
approval returns its stored outcome and cannot increment Inventory or create a
second Transaction. Cancellation applies to a separate stored approval and records
no Inventory, Shopping, or Transaction business mutation. The reset is B1's
explicit, household-scoped reset procedure followed by its read-back verification;
it restores all 31 baseline business records before another destructive rehearsal.

This contract is backend/Cosmos acceptance until C2 demonstrates the same journey
on an Android physical device through the deployed cloud API, including refresh or
reopen read-back. It is not evidence for new Inventory items, unlinked Shopping
rows, over-pending quantities, different units, direct Shopping additions, price
options, or media attachments.

The shared `demo-household` is not a disposable test partition. Before any reset or
rehearsal write, obtain explicit authorization from the data owner for the records
that may be changed, run and review a complete read-only partition preflight against
the current data, and confirm the approved reset procedure and expected baseline.
Use the isolated `c1-test-*` household for synthetic write acceptance. After an
authorized Demo reset, read back and reconcile the complete 31-record baseline
before continuing; the isolated acceptance script does not authorize or perform a
Demo household reset.

Read-only Demo verification record (2026-11-14): C1 compared the live
`demo-household` with the 31-record manifest and confirmed the forecast/reorder
results: flour and detergent are reorder candidates; four items have forecasts and
three return `insufficient_data`. C1 also reviewed and approved the latest PR #141.
Evidence is recorded in [Issue #126](https://github.com/nestock-app/nestock/issues/126#issuecomment-6870500061).
No Demo records were written, reset, or deleted. Live purchase/retry/reset E2E
remains pending explicit data-owner authorization and is not established by these
read-only checks.

`tests.manual_test_shopping_cosmos` records the isolated backend/Cosmos portion of
this contract in a fresh `c1-test-*` household. Its JSON result includes the core
approval, operation and Transaction identities; it explicitly checks baseline
Inventory quantity 1 / legacy Shopping quantity 2, post-confirmation Inventory
quantity 3 / completed Shopping with the legacy value still 2, one operation-bound
purchase Transaction, a retry without duplicate effects, and a separate cancellation
without a business write.
The isolated fixture deliberately uses the Final Demo's detergent identifiers
(`seed-detergent` and `seed-shopping-detergent`) and values while keeping the write
in its fresh test household. It intentionally does not reset `demo-household`; B1's
reset plus read-back is the separate final-Demo evidence for that last step.

## Current limits and Demo prerequisites

The Agent/resume backend flow supports existing Inventory targets linked from
pending Shopping entries and stored `create_item` lines that create a new Inventory
target. Both use an explicit positive whole-number purchase quantity independent
of any legacy Shopping quantity and require the same unit. C1's builder also
supports the `same_item` purchase mapping without alias learning, but the Agent cannot issue
that card until A1 completes the approval and resolver integration in Issues #90
and #175. C1's builder supports removal, but the Agent has no removal card. Unit
conversion, media attachment retention, and historical purchase-time entry remain
unsupported in the Agent/App flow; they do not fall back to unapproved writes.

The route is backend integration, not App E2E: C2 still needs to connect the Android
approval-card and retry/cancel flows to the Agent API. Production evidence also still
requires B1's resettable Demo baseline and durable Cosmos read-back, plus B2's
Transaction/read-side validation. The isolated `c1-test-*` check below is not the
Demo baseline and must not be presented as Android acceptance.

`ShoppingWriteBuilder` supports removal and new targets when an approved stored-card
resolver supplies the exact values and B1 stable ID. The backend integration path is
covered offline; Android card presentation and refresh/reopen acceptance remain C2
work. Optional media retention remains disabled in production composition pending
A2/B1 integration. Non-Shopping event
scope, media detach/cleanup, oversized-card
splitting and broader live conflict/reconciliation validation remain cross-owner
work. Until those decisions and C2 flow are complete, do not claim #27 or #90 is
complete end-to-end. Retain **provisional per D-3** in an integration PR while the
decision index remains stale; do not change shared contracts here.

## Media attachment retention (#98, provisional per PR #97)

`ResolvedShoppingChanges.media_attachments` accepts selected purchase bindings
`MediaAttachment(operation_id, input_id, image_ref, mapping_etag)`. This is a
provisional internal handoff, not an App/wire schema. `image_ref` must be shown in
the stored operation values and the exact `input:<input_id>` ETag must be one of
its targets. The formal A1 `CardInputResolver` and production composition do not yet
provide these bindings, so purchase-card attachment remains disabled there.

Media is opt-in through `ShoppingWriteBuilder(..., media_retention=...)`.
`B1MediaRetentionPlanner(mapping_storage)` uses PR #97's completed, household-
scoped lookup and `build_media_mapping_retention_update(..., ttl_seconds=None)`.
It plans only: mapping retention is never written on its own. PR #97 is now merged
in main (11e078f).

A completed mapping's retention replace joins the shared household batch with
the actual Inventory image_ref attachment, Shopping, Transaction, event, receipt
and approval completion. Mapping ETag, household, status, reference and output
identity are checked. Repeated bindings share one mapping update; conflicting
images for one Inventory target reject the group. Mapping writes count against
the same 100-action ceiling. Cancellation/unselected purchases retain orphan TTL;
conditional conflicts roll back business writes and retention together. Image
references remain opaque; no binary, base64, path parsing or Blob upload is added.

Replacing an existing different image, detaching media, and resetting finite TTL
are deliberately refused/deferred. PR #97 has no authoritative attachment-set or
last-reference query: a removed Shopping row cannot prove no other business
record still references an image. B1/A2/C1 must agree that source and same-batch
unlink semantics before a detach can reinstate orphan cleanup. No reference-count
schema or assumption that removal of Shopping detaches Inventory is invented.

Only the mapping TTL is covered here. Blob retention/deletion and A2 processing
composition are separate owner work; a mapping update does not prove Blob cleanup.

## Post-commit media handoff (#98, C1 orchestration draft)

When a purchase has an attachment, `ShoppingWriteBuilder` requires a
`MediaRetentionWorkItemWriter`. `B1MediaRetentionWorkItemAdapter` now maps that
batch seam to the merged #191 `build_pending_media_retention_work_item(...)` helper,
so one deterministic pending record joins the purchase operation's same-household
batch with the Inventory attachment, mapping retention update, receipt and completed
approval. Its stable record ID is `media_retention:` plus the SHA-256 of the stable
`operation_id`; timestamps come from the stored approval's `decided_at`. B1's
canonical pending record starts with `next_attempt_at=decided_at`. It stores
`approval_id` and `image_ref`, but not `input_id` or mapping ETag.

`B1MediaRetentionWorkItemAdapter` also maps the worker onto #191's
household-scoped `list_due(..., now, limit)` and snapshot/ETag `transition(...)` port.
`MediaRetentionRetryWorker` processes only due pending items. It invokes A2's
idempotent `retain_media`; success marks the item complete. Exceptions use A2's
`classify_media_retention_error` result for transient/permanent classification,
safe `error_code` and suggested backoff (5/10/30/60 seconds) with bounded jitter.
The worker keeps its three-retry limit. Missing or purged media, other permanent
errors, and exhausted retries become durable `needs_attention` items without
changing the already-completed purchase. Retries never route back through
`ApprovalService` or replay domain writes.

`MediaRetentionPostCommitTrigger.after_resume(...)` is C1's post-commit orchestration
seam. The A1/API caller passes the `ResumeResult` and household only after
`ApprovalService.resume()` returns. Non-written outcomes are ignored; completed and
already-completed outcomes run the household-scoped on-demand worker. Worker/query
failures are logged and leave the durable item pending, so they do not turn a
completed purchase into an API failure or replay its domain writes.

The adapter, worker and trigger are not yet invoked by production `/agent/resume`
composition. A1/API still needs to call this C1 seam after resume, and production
composition must supply the B1 work-item store and A2 `retain_media` port. Do not
treat the seam alone as purchase attachment or Blob lifecycle end-to-end acceptance.

## Isolated live acceptance

`backend/tests/manual_test_shopping_cosmos.py` is an opt-in acceptance script,
never part of automatic pytest. Run it only with explicit user approval and a
fresh `c1-test-*` household. It refuses a reused household or a container without
`defaultTtl=-1`, does not change Azure configuration, and retains synthetic records
for B1 review. It uses one SDK client/container for reads and writes.

The script uses #97 helper/mapping dependencies, now merged in main. Its resolver
is explicitly test-only:
existing purchases use #95's stored values; the new-target/media fixture does not
claim to be the finalized A1 wire schema. It checks pending/no early stock change,
complete purchase, actual Transaction/event, stored response values, stable retry,
new-item creation via B1's stable helper, and retention with the new attachment.
It does not establish Agent/App end-to-end or A2 Blob lifecycle acceptance.

Example from the backend directory, after Azure CLI login:

```sh
python -m tests.manual_test_shopping_cosmos --execute --household c1-test-UNIQUE-RUN-ID
```

On 2026-11-11 the C1 live script completed **18/18 checks** after CLI login
with the service-wide Cosmos scope. The only written household was
`c1-test-20261111T090129Z-c2b285db`. Synthetic records were retained for B1 review.
The run used the isolated #64/#97 dependencies, the actual C1 builder and B1
SDK/repository, A1 ApprovalService and a **test-only** A1 resolver.

Validated: pending card persisted without stock changes, confirmed existing stock
1 + 2 = 3, Shopping completion, one actual Transaction/event, stored quantity/unit
outcome, already_completed retry without duplicate effects, new Inventory created
only after approval using B1's stable helper, and media attachment plus retention
updated together. The new-item retry produced no duplicate Inventory/Transactions.
This does not cover production Agent/App parsing, A2 upload/Blob cleanup, media
detach, every live conflict/reconciliation case, or a final builder ownership decision.

Authentication note (2026-11-11): the account-specific login scope returned
AADSTS65002 (Azure CLI / first-party API preauthorization), so use the service
scope `https://cosmos.azure.com/.default` for CLI login/token validation. The
acceptance script temporarily sets the installed SDK's supported
`AZURE_COSMOS_AAD_SCOPE_OVERRIDE` to that same value and restores it afterward;
no persistent CLI settings, credentials, roles or .env values are changed. A
successful browser portal session is not proof that CLI token acquisition works.
