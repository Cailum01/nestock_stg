# C1 shopping plans and approval write builder

This module implements purchase/removal domain planning and batch assembly. It
performs no persistent writes itself and is not wired to routes, Agent tools or
application composition. `POST /agent/resume` remains disabled. Plans are not
approval. A1's `ApprovalService` authorizes selection, commits and recovers through
B1's `CosmosWriteRepository`; the A1 stored-proposal resolver is still required.

## Domain semantics

Per [Issue #27](https://github.com/nestock-app/nestock/issues/27), Shopping quantity
is the original plan. Planning two bottles and actually buying one completes this
Shopping task, keeps the planned quantity at two, adds one to Inventory and creates
one purchase Transaction. No remaining pending intent is inferred. Completed
entries disappear from the active list without hard deletion.

Per [Issue #63](https://github.com/nestock-app/nestock/issues/63), counts use integer
arithmetic. Shopping planned quantity/unit can be absent; no value of one or default
unit is invented. Actual purchase quantity must be explicit and positive. Fractional,
nonfinite, boolean and string counts fail. Integral floats are accepted only for
compatibility with the old model while B1's integer/optional model PR #64 is pending.
An actual unit must be resolved and match the Inventory target and any planned unit.
There is no unit conversion or automatic purchase beyond an existing plan.

Manual removal only marks a valid pending Shopping entry with storage-only
`deleted_at`, preserving the public pending/completed status. It never updates
Inventory, creates a purchase Transaction, or reverses earlier effects.

## Plans and new targets (#90)

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

A1 resolves ambiguous existing-item matches and any alias approval before handing
inputs to C1. This builder does not execute item-match cards or standalone gifts /
add_stock without a Shopping entry. Those need A1's card lifecycle and B1's event
scope agreement; no dummy Shopping ID or event field is invented here.

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

Issue #113 proposes treating Transaction.created_at as recording time in every
case. That differs from the #60 event-time handoff above when purchased_at is
historical; it agrees only with the no-purchased_at fallback. The newer B1 reply
in #113 also differs from B1's earlier #60 confirmation and design section 9.1.
Until A1/B1/B2/C1 reconcile these statements, this implementation retains the
existing split; this README does not settle the shared Transaction contract or
enable forecasting. `recorded_at` is a planner argument, not a Transaction field;
ChangeEvent records both occurred_at and recorded_at. A1's pending #71 helper
resolves dates, #104 passes stored purchased_at through, and #96's current card
producer does not supply a historical purchased_at. Helper/resolver support is
not evidence that historical-date entry is wired through the Agent/App.

## Batch builder (#27 / #60)

`ShoppingWriteBuilder(reader, resolver)` implements A1's `DomainWriteBuilder` port.
Its `build(ApprovalRecord)` returns `DomainWritePlan`, without executing a write.
`reader` uses B1's household-scoped point-read shape. The injected A1
`ShoppingInputResolver.resolve(record)` returns `ResolvedShoppingChanges` with
`purchases` and/or `RemovalInput`s. This is an internal typed handoff, not a final
public wire schema. It supports single-action and batch-action cards only.

A1's resolver must validate supported actions and bind every actual quantity,
unit, name, location, resolved target, transaction/new-item ID and purchase time
to the exact server-stored proposal. It must use fixed selected operation IDs,
never resume-edited values, new retry IDs or fresh matching results. The pending
save-time trial uses created_at temporarily for count-only planning; it authorizes
nothing. A1's stored-card resolver is supplied by pending PR #104; production
composition is proposed in pending PR #96. Neither is enabled by this C1 module.

C1 additionally verifies the exact selected ID set, unique Shopping targets,
card-bound source ETags, household, current source snapshots and absence of a new
target. Deleted targets stop. Resolver ordering is canonicalized to card order.
All source replacements preserve storage-only metadata while stripping Cosmos
system properties. The final ETag/create conditions close the race after reads.

B1's mapping is used unchanged:

- Per operation: Shopping conditional replacement, Transaction create and
  `change_event_create`; a new Inventory create is in its originating operation.
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

The real name-only Pydantic test is explicitly skipped on main's old required
fields and runs with #64's actual schema. Joint validation applies only #64's model
and model-test patch to current main plus C1, retaining the newer ChangeEvent model.
No B1 files are edited in this working tree. Coordinate review/merge with B1; keep
#63 open until both sides land. See the linked Issues and pull requests for current integration evidence and owner work.

Remaining: A1 stored-proposal resolver, Agent card/resume composition and oversized
card splitting; production stable-ID helper composition, non-Shopping event scope
and broader live failure/reconciliation validation; C2 actual card flows. Until these are integrated, no claim that #27 or
#90 is complete end-to-end. Retain **provisional per D-3** in an integration PR
while the decision index remains stale; do not change shared contracts here.

## Media attachment retention (#98, provisional per PR #97)

`ResolvedShoppingChanges.media_attachments` accepts selected purchase bindings
`MediaAttachment(operation_id, input_id, image_ref, mapping_etag)`. This is a
provisional internal handoff, not an App/wire schema. `image_ref` must be shown in
the stored operation values and the exact `input:<input_id>` ETag must be one of
its targets. A2/B1 must confirm this handoff before enabling it in composition.

Media is opt-in through `ShoppingWriteBuilder(..., media_retention=...)`.
`B1MediaRetentionPlanner(mapping_storage)` uses PR #97's completed, household-
scoped lookup and `build_media_mapping_retention_update(..., ttl_seconds=None)`.
It plans only: mapping retention is never written on its own. PR #97 is now merged
in main (11e078f); enabling attachment requires its port and explicit composition.

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
