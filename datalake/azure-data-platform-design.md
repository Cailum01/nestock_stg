# Nestock Azure Data Platform Design

**Status:** B1 design proposal for review
**Scope:** Persistence and storage boundaries for multimodal user input, inventory,
transactions, descriptive notes, and future Agent workflows
**Implementation status:** Documentation only. Skeleton v1 must remain offline and
must not connect to Azure or implement persistence.

This document records initial design decisions and their adjustment points. It is
an implementation guide for AI-assisted development, not a second API schema.
Backend Pydantic models remain the source of truth for domain and HTTP contracts.
Any proposed storage envelope below is internal to persistence unless explicitly
approved as a domain-model change.

## 1. Decisions at a glance

| Area | Initial decision | Deliberate adjustment point |
| --- | --- | --- |
| Business database | Azure Cosmos DB for NoSQL | Revisit container and partition strategy before production scale |
| Original user media | Private Azure Blob Storage; required for uploaded binary files | Container layout, region, replication, and lifecycle policy are environment settings |
| Text input and notes | Store as scoped records in Cosmos when the user action requires persistence | Separate Agent conversation history remains an A1 decision |
| Data Lake | Do not require or deploy it for the first implementation | Add ADLS Gen2 only when analytics, long-term event replay, or a formal audit requirement justifies it |
| Multimodal processing | Store original once; persist task metadata and derived artifacts separately | A1/A2 select Agent runtime, recognition providers, and orchestration |
| Inventory writes | Persist confirmed changes, transaction, idempotency receipt, and change event atomically within one household partition | Cross-domain purchase workflows require C1 agreement |
| Transaction history | B2 reads raw transaction records; B1 does not pre-aggregate | Query contract and proposed feature-only transaction schema require cross-owner approval |
| Transaction-to-item resolution | Append an immutable link record; never rewrite the source transaction | B2 reads link records for attribution, but never reads `change_event` |
| Inventory removal | Soft-delete inventory records; retain IDs and transaction history | Distinct from personal-data erasure and Blob retention |
| Family member preferences | Keep household inventory shared and store `like` / `dislike` per member and item; missing preference means `normal` | Requires an approved identity/membership feature; excluded from Skeleton v1 |
| Forecast evaluation | Store internal B2 prediction/evaluation records in Cosmos | B2 owns schema and algorithm; records are not shown to users |
| Authorization | No client-supplied value or model output authorizes a write; approval is validated server-side | Approval protocol is jointly owned with A1 and affected domain owners |
| Retention and limits | Use centralized, environment-configured pilot defaults; do not hard-code policy throughout services | Product/legal decisions can change defaults without changing the storage model |

### 1.1 Pilot defaults proposed by this document

These are conservative starting values for a future pilot, not legal retention
promises or production capacity guarantees. Keep them in one versioned
configuration/policy location and make them overrideable per environment.

| Setting | Initial default | Notes |
| --- | --- | --- |
| Unattached, abandoned upload | Delete after 24 hours | Uploads not associated with a valid task |
| Original media after successful processing | Retain for 90 days | Delete earlier on a valid user deletion request |
| Media explicitly attached to a saved business record | Retain while the attachment is active | Deleting/detaching the record must apply the documented deletion workflow |
| Text notes and confirmed domain records | Retain until user deletion or an approved product retention policy says otherwise | Do not apply the raw-media TTL to notes or transactions |
| Image size | 20 MiB maximum | Validate actual bytes and decoded format |
| Audio size / duration | 25 MiB / 10 minutes maximum | Limits are configurable; reject unsupported inputs explicitly |
| Video size / duration | 100 MiB / 2 minutes maximum | Limits are configurable; process asynchronously |
| Text input | 32 KiB per submitted message | Conversation/history retention is not implied by this limit |

If policy or provider constraints require different values, change the configuration
and this table together. Never silently truncate user input or delete data earlier
than the active policy permits.

## 2. Scope and ownership boundaries

This design covers B1-owned persistence foundations for inventory, transaction
records, user-submitted source records, and their references to stored media. It
also defines extension points that other owners can use without making their
decisions for them. It proposes a future persistence shape for member-specific
item preferences, but does not implement household accounts or membership.

- **A1** owns Agent graph/state, checkpoints, approval protocol, and task
  orchestration. The persistence design must not expose Cosmos SDK details to
  Agent widgets or tools.
- **A2** owns image/video/audio processing integrations, normalization, and
  provider-specific derived content.
- **B2** owns forecast and transaction-history aggregation behavior. B2 reads
  transaction records (and explicit transaction-to-item links for attribution)
  and computes its own aggregates; B1 provides source records and query filters,
  not forecast-ready summaries.
- **C1** owns ShoppingService behavior and `complete_purchase`. The initial
  household partition proposal may support same-partition atomic operations, but
  it does not decide the shopping workflow or its write contract.
- **C2** owns frontend presentation and final wire mapping.

Normal API routes and Agent tools must call the same domain services. Business
rules belong in `backend/app/services`, not in routes, storage repositories, or
Agent adapters.

### Out of scope for Skeleton v1

- Connecting to cloud services for writes or media/analytics processing.
  Exception: the explicitly approved, opt-in, Entra-authenticated, read-only
  Cosmos adapter for Inventory and Shopping may be used for development
  validation; it is disabled by default and does not authorize users or enable
  transaction-history reads.
- Connecting the backend to Blob Storage, ADLS Gen2, Azure AI services, queues,
  or any other cloud service beyond that read-only Cosmos exception.
- Implementing authentication, accounts, household membership, member
  preferences, real uploads, real Agent execution, task resume, approval
  persistence, or domain mutations.
- Choosing a final Agent conversation/checkpoint schema or final wire protocol.
- Implementing forecasting, shopping orchestration, checkout, or payment.
- Treating AI-generated observations as confirmed inventory facts.

## 3. Product and data invariants

1. `household_id` scopes every household-owned record and every read/write.
   Development currently uses `demo-household`; it is not an authentication
   mechanism.
2. `InventoryItem.quantity` is the latest user-confirmed quantity. Predictions,
   OCR, transcription, video analysis, and model inference never overwrite it.
3. Inventory has no category. `location` remains human-readable free text.
   `image_ref` is opaque external metadata, never binary data or Base64.
4. Shopping status remains `pending` / `completed`. Do not persist product price
   or place orders/payments.
5. Transactions have a stable `operation_id` and a purchase, consumption, or
   adjustment reason. Storage does not settle B2 aggregation or C1 workflows.
6. User notes may be independent context or explicitly linked to an input,
   inventory item, transaction, or operation. A note is not itself permission to
   mutate a linked record.
7. Original files, extracted/transcribed content, Agent task state, and confirmed
   business records are separate data classes with separate retention rules.
8. A write requires backend validation and applicable human approval. A client
   boolean such as `approved: true` and generic Agent `values` are never sufficient.
9. Units are system-defined. The client cannot set or change an item's unit.
   Transaction quantities use the applicable canonical item unit; transactions
   do not store a separate `unit` field.
10. Removing an inventory item is a soft delete. It preserves the item identity
    and transaction history; it is not a request to erase personal data.
11. Inventory is shared at the household level. A member's preference is personal
    metadata and never changes shared stock, another member's preference, or
    write authorization.

## 4. Logical architecture and data flow

```text
Flutter client
  ├─ Text and note submission ──> FastAPI / shared domain services
  └─ Binary upload ──> short-lived, single-object upload authorization
                         └─> private Blob Storage

FastAPI / shared domain services
  ├─ Cosmos DB for NoSQL: scoped records, task metadata, confirmed domain data
  ├─ Blob Storage: original media and large derived artifacts
  └─ Future processing adapter / queue (owner decision): OCR, transcription,
     image/video understanding, and other Agent work

Future optional analytics path
  Cosmos append-only change-event documents
      └─ Change Feed consumer
           └─ ADLS Gen2 (Data Lake; enable only when justified)
```

### 4.1 Input-to-result lifecycle

1. Accept a text message, note, or media upload request and assign stable
   `input_id` and `task_id` identities. Validate household scope, declared media
   type, actual content, configured limits, and request idempotency.
2. For binary media, upload the original bytes to a private Blob object. The
   client receives only a short-lived, narrowly scoped upload authorization if
   direct upload is used. Do not route large media through API memory buffers.
3. Save a Cosmos input record containing source metadata and an opaque blob
   reference, or the submitted text when text retention is intended. Never put
   binary bytes in Cosmos.
4. A1/A2-owned processing may create derived text or artifacts (for example
   transcription, OCR, or extracted observations). Store large derived output as
   Blob objects; keep bounded structured results and references in Cosmos.
5. The Agent may answer a read-only question, ask a follow-up, provide a
   recommendation, or propose a mutation. A proposed mutation is not persisted.
6. For a mutation, resolve the exact target and values, show those values to the
   user, obtain applicable approval, validate the unchanged proposal on the
   backend, and then persist the operation idempotently.
7. Save provenance links from the operation/event to the relevant input IDs and
   derived artifact IDs. Do not copy entire source files into the business record.
8. Apply retention and deletion policies independently to source media, derived
   artifacts, notes, tasks, business records, and optional lake projections.

Queries and responses need not create permanent user records. A1/product policy
must define whether and how ordinary conversational messages are retained.

## 5. Storage responsibilities

| Data | Primary location | Cosmos contents | Blob contents |
| --- | --- | --- | --- |
| Inventory and shopping records | Cosmos DB | Authoritative structured domain records | None |
| Transactions | Cosmos DB | Authoritative append-only transaction records | None |
| Future household membership and item preference | Cosmos DB | Household membership metadata and separate per-member preference records; never credentials | None |
| Transaction-to-item resolution | Cosmos DB | Append-only links from feature-only transactions to selected items | None |
| Text input / descriptive note | Cosmos DB | Text, scope, timestamps, retention class, optional links | None |
| Original photo, audio, video, or document | Blob Storage | Opaque blob reference, media metadata, ownership/scope, status | Original bytes |
| Transcription, OCR, or bounded extraction | Cosmos for small structured output | Summary, typed extraction, artifact reference, provenance | Large text/artifact output when needed |
| Agent task/checkpoint state | A1-selected persistence boundary | B1 may persist storage metadata/operation references only | Large intermediate artifacts |
| Business operation/change event | Cosmos DB | Immutable event and operation identity in the same partition as the write | None |
| Analytics/replay projection | Optional ADLS Gen2 | Source event remains authoritative in Cosmos | Lake files are derived, not authoritative |

### 5.1 Why Blob and Data Lake are not both mandatory

Blob Storage is needed for original and large binary files. Data Lake is not
required merely because the application accepts video, photos, voice, or text.
Those source files can live in Blob Storage and business records/events can live
in Cosmos.

Enable ADLS Gen2 when there is a confirmed need for large-scale historical
analytics, data-science workloads, long-lived event replay, or a formal audit
retention/reporting requirement that is better served by lake storage. Until then,
do not create a lake pipeline or duplicate every file into a second store.

ADLS Gen2 uses Blob Storage with hierarchical namespace enabled. It is a distinct
logical data-lake role and may affect storage-account choices and enabled
capabilities. Decide whether to use the same or a separate storage account only
when the lake is approved; do not assume two independently required copies of
every object.

## 6. Cosmos DB for NoSQL design

### 6.1 Initial container and partition proposal

Start with one logical `household-data` container using `/household_id` as the
partition key for household-owned records. Store a `record_type` discriminator
and a stable Cosmos `id` on each record. This keeps the initial inventory update,
transaction, operation receipt, and change event eligible for a same-container,
same-logical-partition transactional batch.

This is a starting point for the expected household-scoped workload, not a
forever-scale guarantee. A household partition can become a storage or throughput
hotspot. Measure real workload before adding containers or changing the partition
key. A partition-key migration requires an explicit backfill, dual-read/write
plan, verification, and rollback strategy; never silently change it.

All data access must include the household partition key. Repositories must
validate that the household in the authenticated/server-side request context
matches the record scope. Do not trust an arbitrary client-provided household ID
as authorization.

### 6.2 Record types and identity

The following are proposed storage concepts, not final public models:

| `record_type` | Purpose | Mutability |
| --- | --- | --- |
| `inventory_item` | Confirmed inventory state | Updated only by validated confirmed operations |
| `shopping_item` | Shopping state; C1 owns service behavior | Updated only by validated domain operations |
| `transaction` | Purchase, consumption, or adjustment fact; proposed `item_id` / `feature_keywords` alternative is pending unanimous contract approval | Append-only |
| `transaction_item_link` | Proposed attribution of a previously feature-only transaction to an item | Append-only; requires shared contract approval |
| `household_membership` | Future association between a stable member identity and a household | Membership lifecycle is not implemented; identity and roles require a separate owner-approved design |
| `item_preference` | A member's preference for a shared inventory item | One current preference per member/item; omit the record for the default `normal` |
| `user_input` | Text or reference to original media | Metadata may change for processing/deletion |
| `note` | Descriptive user-provided context | Editable/deletable under explicit user action |
| `agent_task_metadata` | Bounded task status and input/artifact references | State transitions validated; A1 owns runtime semantics |
| `pending_approval` | Server-side confirmation proposal and state; A1 owns lifecycle semantics | Conditional state transition; expiry/TTL follows D-1 |
| `operation_receipt` | Idempotency and stored operation outcome | Immutable after successful commit |
| `change_event` | Business change provenance and future export source | Append-only |
| `forecast_snapshot` | Withdrawn B2 P0 proposal for point-in-time forecast persistence | Not used in P0; B2 uses read-only historical backtesting |
| `sufficiency_check` | P1 proposal for a user's answer to a “still enough?” prompt | Out of current B1 scope; schema and write policy require B2/A1/B1 agreement |
| `forecast_evaluation` | Possible future evaluation record if a later product need requires persisted outcomes | Not used in P0; any future write requires a new B2 proposal and agreement on authorization and read-only API boundaries |

Use namespaced IDs to avoid collisions, for example
`inventory:<item_id>`, `transaction:<transaction_id>`,
`input:<input_id>`, `note:<note_id>`, `operation:<operation_id>`, and
`event:<event_id>`. Do not use user-provided filenames as Cosmos IDs or Blob paths.
Do not expose Cosmos `id`, partition implementation, or storage envelope as
public API fields unless owners approve a contract change.

#### Pending approval storage (provisional per D-2)

B1 proposes storing each pending approval as a `pending_approval` record in the
existing `household-data` container, partitioned by its `household_id`. The
record holds the server-resolved proposal shown to the user and its lifecycle
state; A1 owns the approval protocol, validation, and response semantics. This
placement keeps the record in the household partition and can participate in
same-container transactional batches where D-3 requires it. Use ETag-conditional
state changes so a confirmation cannot be consumed twice. Configure item-level
Cosmos TTL from the D-1 expiry setting; do not hard-code an expiry duration while
D-1 is open. This is a storage proposal, not approval to add resume endpoints or
write tools to Skeleton v1.

#### Transaction contract change (B2 proposal; unanimous approval pending)

The current model requires `item_id` and has no `feature_keywords` or
`recorded_at`. B2 proposes making `item_id` optional, adding a
`feature_keywords` snapshot, and recording a separate `recorded_at`. These
changes require unanimous approval of the shared domain/API contract. Validation
would require at least one of:

- a non-empty `item_id`; or
- a non-empty `feature_keywords` snapshot.

Both may be present. `feature_keywords` contains the exact feature terms
normalized by A2 and is immutable after creation. Reject empty strings and
duplicate keywords in backend validation. B2's P0 feature lookup supplies one
keyword and uses exact equality; no fuzzy, synonym, AND, or OR matching is part
of this query.

This is a B2 proposal, not an approved shared contract. A1 must decide whether
the Agent can record an unresolved-item Transaction with keywords or must always
clarify the item with the user before recording it. If A1 requires clarification
first, the existing required `item_id` contract remains sufficient and the
feature-only Transaction/link work below is not needed. If the feature-only path
is selected, all owners must approve the schema, link lifecycle, and migration
before changing the Pydantic model, OpenAPI contract, or persistence behavior.

B2 proposes that `created_at` represent the UTC business occurrence time (such
as the purchase date), even when reported later, and that storage-only
`recorded_at` represent when the system accepts the Transaction. B2 would filter
and order by `created_at`; late-link reconciliation would compare it with the
item's quantity-confirmation timestamp. The proposal would reject a
`created_at` later than `recorded_at`, with configurable clock-skew tolerance.
These timestamp semantics and how to populate `recorded_at` on existing records
remain subject to shared approval and a migration plan; do not enforce them yet.

### 6.3 Notes and relationships

Support both:

- **Standalone notes**: household-scoped descriptive context that is not attached
  to a business entity.
- **Linked notes**: a note with explicit references to one or more known records,
  inputs, or operations.

Store a note as its own record rather than a free-form field copied into every
linked entity. A note may include `note_id`, `household_id`, text, timestamps,
retention class, and typed reference(s) such as `{type, id}`. These are storage
design fields only; agree the public domain model with affected owners before
adding them to Pydantic models.

Do not infer note links from text, silently rewrite notes into facts, or treat a
note as authorization. If a user says a fact that changes inventory, the Agent
must resolve and display the proposed change and follow HITL rules.

### 6.4 Future household membership and per-member preferences

The current product has no authentication, household account, or membership
feature. The following is a forward-compatible storage proposal only; it does
not make the current `demo-household` scope a real security boundary.

Keep the inventory item as one shared household record. Store personal
preferences in separate records rather than embedding a per-member map in
`inventory_item`, so a preference update does not contend with or mutate shared
quantity. A future `item_preference` record may contain:

```text
record_type = "item_preference"
household_id
member_id
item_id
value = "like" | "dislike"
created_at
updated_at
```

The logical identity is `(household_id, member_id, item_id)`, enforced through a
deterministic document ID or an equivalent conditional write. The effective
preference is `normal` when no preference record exists; persist only explicit
`like` or `dislike` values. Setting a preference back to `normal` removes the
current preference record. If a future audit requirement needs preference-change
history, add a separate append-only event rather than retaining stale current
state in this record.

Use an opaque, stable `member_id` issued by the future identity/membership
system. Do not use email addresses, display names, or client-supplied IDs as
authorization evidence or storage keys. A future `household_membership` record
must establish that the member is active in the requested household before
reading or writing that member's preferences. The server derives household and
member scope from trusted authentication context; it must not trust a submitted
`household_id` or `member_id` by itself.

With the proposed `/household_id` partition, membership and preference records
can reside in the same household partition as the shared inventory. Preference
queries must always include both household and member scope. A member can update
their own preference only, subject to a future authorization policy; another
member's preference is not overwritten or averaged. Roles, invitation flows,
visibility rules, and membership-removal behavior remain undecided and require
agreement with the identity/product owners before implementation.

Soft-deleting a shared item does not automatically erase transaction history.
Whether to retain or remove associated member preferences on soft deletion is a
product decision; a reasonable default is to retain them with the item and make
them effective again if the same item identity is restored. A member-removal or
personal-data-erasure workflow must separately define cleanup of that member's
preferences without deleting household inventory or other members' data.

### 6.5 Atomic inventory writes

For one approved inventory operation, use a Cosmos transactional batch containing
the applicable operations in the `household-data` container and household
partition:

1. Create an `operation_receipt` with the stable operation ID and a fingerprint
   of the approved canonical request.
2. Update the inventory record using optimistic concurrency (ETag/conditional
   update) so concurrent changes cannot silently overwrite one another.
3. Create the corresponding immutable `transaction`.
4. Create an immutable `change_event` linking the operation to source input IDs
   and describing the confirmed change.

Either the batch succeeds as a unit or none of its writes are committed. If the
SDK/container constraints or final schema cannot support this boundary, stop and
revise the design before implementing a partial write path.

For the same operation ID and same canonical request fingerprint, return the
stored result without applying the mutation again. The same operation ID with
different values is a conflict, not a retry; require a new proposal and approval.
Do not rely only on a separate “check then write,” which is race-prone.

The `(household_id, operation_id)` pair is the idempotency key: store the receipt
in the same household partition with a deterministic ID derived from the
operation ID, and create it in the same transactional batch as all related
writes. A1 confirms the Agent creates the operation ID before pausing and stores
it server-side; the App cannot supply it. Editing any approved value requires a
new operation ID. On a retry, the same ID and canonical payload returns
`already_applied` as a successful outcome without repeating writes. Reusing the
ID with a different payload returns an operation conflict so A1 can request a
new approval. If the original response was lost after commit, the receipt makes
the retry resolve to `already_applied`; if the batch did not commit, the retry
can execute the batch using the same ID. A1 owns translating this repository
outcome into the prior user-facing result.

Transaction signs, unit conversions, and quantity arithmetic policy remain
domain decisions; persistence must store the already validated domain operation,
not invent those rules.

#### A1 coordination: approval and repository boundary

A1 agrees that approval is bound to the exact server-resolved operations and
relevant values presented to the user. A changed target, quantity, location,
selected purchase option, or other relevant value requires fresh approval.
Approval is checked against the backend-stored proposal; a client-supplied
`approved` flag or mutable values never authorize persistence.

The domain service owns business validation and approval checks. The B1
repository owns persistence primitives only, including reads/writes, conditional
updates, and transactional batches; it does not interpret approval. Reusing an
`operation_id` for the same logical write must not duplicate mutations. Reuse
with different canonical values is a conflict and requires a newly resolved
proposal and approval.

Still deferred to A1's interrupt/resume experiment are who creates the
confirmation-card expiry, pause-state storage, and the behavior of multi-item
approval groups. A1 has confirmed that a rejection performs no write and returns
“已取消”; cancelling after a committed write does not roll it back. A failed or
timed-out attempt keeps the same confirmation and operation ID available for
retry; if the user cancels instead, no later write is attempted. Invalid or
cross-household confirmation IDs are rejected by A1 before B1 persistence is
called.

#### Multi-operation confirmation batch (provisional per D-3)

Cosmos transactional batches can provide all-or-nothing writes when all
operations target one container and one logical partition. The current
`household-data` container and `/household_id` partition therefore support D-3's
proposed atomic confirmation group when every affected record belongs to that
household. A feature-transaction link and its eligible inventory quantity update
must be in the same confirmation and batch; do not create a second Transaction.
Each logical item operation keeps its own Agent-issued `operation_id` and
idempotency receipt. The batch adapter must create all relevant receipts and
domain changes together.

This is **provisional per D-3**. Do not promise atomicity for records in another
container, partition, or service. C1 must confirm which Shopping/Inventory/
Transaction records `complete_purchase` includes. Before enabling the SDK
adapter, validate the final operation count, request size, and execution-time
limits against Cosmos transactional-batch limits; reject an oversized group
before any write rather than splitting it into success-shaped partial writes.
Selection semantics and the `ResumeRequest` representation remain open under
D-3.

Shopping operations and `complete_purchase` need C1's decision on which records
belong in the same atomic batch. Do not claim cross-container, cross-partition,
or cross-service atomicity.

#### C1 purchase workflow integration (provisional)

C1's response to [Issue #27](https://github.com/nestock-app/nestock/issues/27)
proposes the following boundary for a purchase already resolved to an existing
inventory item:

- Read the pending `ShoppingItem`, its resolved `InventoryItem`, and the current
  server-side approval state. Use only the user-reported, approved purchase
  quantity; transaction history does not infer the current purchase amount.
- In one same-container, same-household-partition batch, update the Shopping
  remainder/status and confirmed Inventory quantity, append the purchase
  `Transaction`, and create the applicable operation receipt and change event.
  Reject before writing if the operation cannot fit that boundary; do not split
  it into partially successful writes.
- Keep each operation's server-issued identity and approved selection stable on
  retry. A matching receipt returns the prior outcome; a reused identity with
  different content conflicts. A timeout is not proof of failure.
- Manual removal is a separate Shopping operation; it does not change Inventory
  or create/reverse a purchase Transaction.

These are C1 workflow requirements, not approval to implement writes. The
Shopping quantity meaning after completion, removal persistence semantics,
approval-state participation, multi-operation behavior, and recovery when a
batch has an unexpected partial receipt state still require B1/C1/C2/A1
coordination. Multi-item behavior remains **provisional per D-3**; do not
silently assign a new operation identity to work whose prior outcome is unknown.

#### C2 confirmation UI integration boundary

C2's response to [Issue #32](https://github.com/nestock-app/nestock/issues/32)
distinguishes presentation state from approval and server truth:

- Field edits are local drafts until the user submits a correction. A correction
  is sent to the Agent to resolve a new proposal; it is not approval and does
  not authorize a write.
- Partial selection expresses which displayed operations the user intends to
  approve. The backend must validate those identities against the stored
  proposal; the UI control and local selection state are not authorization.
- A pending card may retain its view/draft state while navigating within the
  active app session. This alone does not require restoring an approval after
  process restart; server-side approval persistence remains governed by D-2.
- On an ambiguous confirmation timeout, the UI retains the proposal and retries
  with the original approval/operation identities. Only an authoritative server
  outcome can show the operation as completed; client state cannot infer whether
  a write committed.

This records C2's presentation requirements, not an authoritative wire model.
Correction, partial-approval, and resume fields still require A1/C2 agreement;
the persistence and atomicity rules remain subject to D-2/D-3.

#### Linking a feature-only transaction to an inventory item

The proposed Transaction contract allows an absent `item_id` when a normalized
`feature_keywords` snapshot is present. The source transaction remains immutable
after creation. Linking it later is a distinct, append-only
`transaction_item_link` record containing at least the household, transaction
ID, selected item ID, stable operation ID, creation time, and the approval
reference needed by the agreed HITL contract. Enforce one active item resolution
per transaction; a correction is a new link record that explicitly supersedes
the previous link rather than editing history.

The link action pauses before persistent mutation. The approval preview must show
the transaction date, amount expressed in the selected item's system-defined
unit, the selected item, and the exact resulting inventory quantity when a
quantity update is proposed.

On approval, compare the transaction's business `created_at` (the purchase/event
time used by B2 queries) with the selected item's last confirmed quantity time:

- If the transaction time is strictly later, atomically append the link record
  and update the confirmed inventory quantity by the transaction delta.
- If it is equal to or earlier than the last confirmed quantity time, append
  only the link record. The purchase is presumed already included in that
  confirmed quantity; do not add it again.

In either case, do not create another Transaction. The operation receipt and B1
change event may record the linking operation; B2 does not consume change events.
Use optimistic concurrency. If the item's quantity or confirmation time changes
after the user saw the preview, do not silently recompute and apply a different
quantity; resolve again and obtain fresh approval.

B2 proposes that resolving a feature-only transaction and applying any eligible
quantity delta be one user confirmation: show the selected item, system-unit
quantity, and resulting confirmed quantity together. This follows the multi-item
approval approach in D-3 and remains **provisional per D-3** until its owners
decide the approval-group and batch semantics.

The existing domain model has `updated_at`, but that timestamp may change for
non-quantity edits. The persistence design therefore needs an authoritative
quantity-confirmation timestamp. Prefer an explicit `quantity_confirmed_at`
field or equivalent persistence metadata; do not use general `updated_at` unless
the contract guarantees it changes only when quantity is confirmed. Agree any
public model change before exposing it through Pydantic/OpenAPI.

### 6.6 Queries and indexing

Expose raw, household-scoped Transaction reads for B2. These are storage queries,
not B1-side aggregation. B2 agrees with all three query shapes:

1. **Household history:** all Transactions for a `household_id`.
2. **Item history:** Transactions for a `household_id` and exact `item_id`,
   including feature-only transactions whose active append-only link resolves
   them to that item. Return each `transaction_id` once; do not rewrite the
   original transaction to populate its item.
3. **Feature history:** Transactions for a `household_id` and one exact
   normalized feature keyword in the transaction's keyword snapshot. Return both
   linked and unmatched transactions and mark each result with its active
   resolution (`resolved_item_id` when linked, otherwise unresolved).

All three queries accept optional `since` and `limit` filters:

- `since` is an inclusive UTC timestamp filter on Transaction `created_at`
  (`created_at >= since`).
- `limit` is an optional positive maximum number of rows.
- When both are provided, apply both: first restrict to `created_at >= since`,
  then order and return at most `limit` rows.
- Sort by `created_at` descending, then `transaction_id` ascending as a stable
  tie-breaker.

This supports date-window use (for example, recent milk usage) and bounded-count
use (for example, the latest N soy sauce purchases). The caller chooses the
window/limit; B1 does not pre-compute consumption or predictions.

Feature keywords are exact-match snapshots after A2's normalization. B1's
repository and Cosmos query do not perform fuzzy, stemming, synonym, or semantic
matching. A query for a normalized feature such as `醬油` can retrieve records
whose snapshot contains exactly `醬油`, including transactions with no `item_id`.
Candidate selection and asking the user to choose remain Agent/domain behavior.
The feature query takes exactly one keyword; P0 does not define multi-keyword
AND/OR behavior.

For item history, B2 proposes the union of direct `item_id == X` transactions
and feature-only transactions whose active link points to X, de-duplicated by
`transaction_id`. An active link is the latest append-only link not superseded
by another link; a transaction has at most one active link. A transaction with
an existing `item_id` cannot be linked. If a link is corrected to Y, the
transaction appears only in Y's history. A link to a soft-deleted item remains
valid. Apply `since`, ordering, and `limit` after union and de-duplication, using
the Transaction's `created_at`, not link time. Return the original Transaction
with a separate resolved item ID and attribution source (`direct` or `link`);
never rewrite the source Transaction. Unmatched feature-only transactions are
excluded from item history and appear only in feature history.

These are B2's proposed link-resolution semantics. Keep them provisional until
the affected owners approve the append-only link contract and D-3 batch behavior.
The proposal, including its alternative of clarifying the item with the user
before recording a Transaction, is tracked in
[Issue #31](https://github.com/nestock-app/nestock/issues/31). Until that choice
and the shared schema are approved, the current B1 repository continues to
require `item_id`; do not implement feature-only writes, link records, or
quantity reconciliation.
For attribution, B2 reads link records along with Transactions; it does not read
`change_event`. Links are metadata, not additional quantity transactions. B1
does not calculate consumption, aggregate history, or materialize B2's
prediction input.

Also support non-B2 household-scoped reads for listing active inventory, loading
records by stable ID, loading notes explicitly linked to a known record, and
loading task/input metadata by stable task or input ID.

B2's response to [Issue #31](https://github.com/nestock-app/nestock/issues/31)
confirms its current forecast adapter reads household Transaction history through
B1's `TransactionQuery` and `CosmosReadRepository`; the current B2 adapter
composition opens a second read-only Cosmos client to the same configured
container. **B1's composition decision is to share one repository/client instance.**
The B1 dependency composition now retains the repository and exposes it through
`get_cosmos_read_repository()`; Inventory and Shopping continue using that same
instance. When integrating B2's service, its `TransactionHistory` Cosmos adapter
should receive this repository rather than create another client or credential.
This avoids duplicate connections and duplicate lifecycle management while
preserving B1's query boundary. The B2 wiring still needs coordination when
integrating PR #16. This does not expose a new public Transaction route or change
the current read-only scope.

Use the partition key and record type in queries. Add composite/range indexes only
when a concrete query and measured workload justify them. Keep indexing policy in
one reviewed configuration. The household Transaction query's
`created_at DESC, transaction_id ASC` ordering requires a Cosmos composite index
with those paths and sort directions before the query is deployed. Keep the
definition in [cosmos-indexing-policy.json](./cosmos-indexing-policy.json) and
apply/verify it on every environment's `household-data` container. The shared
development container has this index applied. Feature-keyword equality plus the
requested sort may require another composite index; add it only after the
Transaction contract is approved and the selected SDK's query plan is verified.

The proposed single `/household_id` partition is supported by B2's current
estimate of approximately 700–3,000 transactions per household per year, or
200–700 over 90 days for a household of up to four people. This is a workload
assumption, not a Cosmos quota guarantee; measure storage, RU consumption, and
hot-partition behavior before production sizing.

Cosmos is not assumed to provide general-purpose full-text or semantic search.
For Transaction feature lookup, use exact equality on the normalized keyword
snapshot; fuzzy matching is performed by A2 before persistence/query. If users
need broad search over notes, OCR, or transcripts, evaluate Azure AI Search as a
separate optional index. It must enforce household filters, support deletion
propagation, and remain a derived index rather than the source of truth.

### 6.7 Units, soft deletion, and forecast evaluation

#### System-defined units

The system owns each inventory item's canonical unit. API and persistence
validation reject a client attempt to set or change it. Transaction has no
`unit` field; its quantity delta is expressed in the associated item's canonical
unit. Before asking for approval, the Agent resolves package descriptions (for
example, one six-bottle carton) to an exact canonical quantity (six bottles) and
shows that quantity to the user. B2 confirms the Agent must use the item's
system-defined unit, not copy a unit supplied in user text into the proposal or
write. Any package conversion must be resolved before approval and the canonical
quantity shown on the confirmation card.

A feature-only Transaction can be stored only when the system can deterministically
resolve its normalized feature to a versioned, system-owned unit definition. Keep
the definition version as persistence metadata, not as a user-settable unit or a
new `Transaction.unit` field. If the canonical quantity/unit cannot be resolved
unambiguously, ask a follow-up question and do not write an untyped quantity.
No general unit-conversion engine is implied.

When forecast inputs are insufficient, B2 returns a status and suggested next
step; it does not persist an inferred adjustment. The Agent may politely ask
whether the user wants to report the current quantity. The user can decline; if
they provide a quantity, it still requires a confirmation card before any write.
Forecast output never replaces confirmed inventory. Any reminder to report a
missed purchase occurs only while the user is using the app; do not add a
scheduler or push notification for this behavior.

#### B2 forecast evaluation (P0 uses read-only backtesting)

Per B2's follow-up on [Issue #31](https://github.com/nestock-app/nestock/issues/31),
`forecast_snapshot` and the previously described `forecast_evaluation` are the
same proposed persistence concept. B2 withdrew that proposal for P0. Instead,
B2 will evaluate its rule-based forecast offline by replaying historical
household Transactions as of past dates and comparing computed consumption with
subsequent purchase/consumption records. This requires only the existing
household-scoped, read-only Transaction history; B1 does not add a record type,
write, index, or forecast-specific repository method for this P0 approach.

The backtest can evaluate daily consumption, but Transactions do not record the
historical confirmed inventory quantity needed to fully evaluate the date stock
ran out. If B2 later needs that outcome or a user-facing accuracy view, it must
submit a new proposal covering required data, retention, authorization, and how
any writes coexist with read-only forecast/API behavior. Do not silently restore
the withdrawn P0 proposal.

The proposed `sufficiency_check` record and “no second confirmation” answer
write are now P1 and out of current B1 scope per B2's follow-up on Issue #31.
Do not implement its schema, reads, or writes as part of the current B1 work.

No forecast persistence schema, stable-ID policy, retention rule, or write
authorization is needed for this P0 backtest. If a later B2 requirement adds
persisted evaluation outcomes, B2 owns the schema and scoring logic; A1 and B1
must agree on authorization and read-only API boundaries before B1 adds storage
mapping, partition-scoped repository primitives, or indexes.

#### Soft-deleted inventory

Removing an item sets a persistence-level deletion marker such as `deleted_at`;
it does not physically delete the item or its Transactions/links. Default active
inventory reads, including lists and point reads, omit soft-deleted items, while
authorized history and feature resolution can still find them. A later confirmed
match may restore the same `item_id`, preserving history for forecasting.

Reusing an old identity does not make its old on-hand quantity current. Require a
fresh user-confirmed quantity or apply only a later, approved, not-yet-counted
Transaction under the §6.5 rule. Keep soft deletion distinct from privacy-driven
personal-data erasure in §7.3; an erasure request follows a separately approved
retention/legal workflow.

## 7. Original media and derived artifacts in Blob Storage

### 7.1 Object layout

Use a private container and server-generated opaque object names. A logical layout
may group objects by environment and household, but it must not expose names,
secrets, or personal data in URLs. Example:

```text
<environment>/<opaque-household-key>/<input-id>/original
<environment>/<opaque-household-key>/<input-id>/derived/<artifact-id>
```

The household key in a path is opaque, not a substitute for authorization.
Cosmos stores the opaque object reference and minimal metadata; the original bytes
remain in Blob. `image_ref` remains an opaque reference and must never contain a
data URL, Base64, or raw file bytes.

### 7.2 Upload security and validation

- Keep containers private; disable anonymous/public access.
- Prefer server-mediated authorization for the current skeleton's future API.
  When direct client upload is later justified, use a short-lived, single-object,
  least-privilege SAS and validate the uploaded object server-side before
  processing. Never put storage account keys in the app.
- Use Managed Identity and least-privilege RBAC for backend and processing
  services. Keep local secrets out of Git and logs.
- Validate household authorization, actual byte count, detected media type,
  decodability, configured size/duration, and quota. A filename extension or
  client MIME type alone is not validation.
- Do not process a blob merely because a client says upload succeeded. Confirm
  object ownership, size, and content before queueing work.
- Define malware scanning and content-safety controls before accepting untrusted
  uploads in a production environment.

### 7.3 Retention and deletion

Apply the pilot retention table in Section 1.1 through centralized lifecycle
configuration and explicit deletion workflows, not scattered per-service timers.
Store creation time, last processing status, retention class, and deletion state
as metadata; do not treat Blob lifecycle rules as the only deletion mechanism.

A user deletion request must define and track effects across Cosmos records,
original and derived Blob objects, optional search indexes, and any enabled lake
projection. Keep only the minimal deletion/tombstone evidence permitted by the
approved policy. Cosmos Change Feed in latest-version mode must not be assumed to
report every intermediate update or hard delete; explicit immutable change events
and a deletion workflow are required when those facts must be preserved.

## 8. Multimodal input and Agent task metadata

### 8.1 Input record concept

Every submitted item receives an `input_id` and is scoped to a household. It may
include:

- `input_id`, `household_id`, `input_kind` (`text`, `image`, `audio`, `video`),
  `created_at`, and `retention_class`.
- Either text content or an opaque reference to the original Blob object.
- Optional `task_id`, explicit note/entity links, and processing state.
- References to derived artifacts with producer and schema/version metadata.

The exact model and API shape remain provisional. Do not add a generic “all
possible fields” model to the domain contract before the owners agree on it.

In its response to [Issue #30](https://github.com/nestock-app/nestock/issues/30),
A2 proposed `user_input` and `derived_artifact` records and an input-processing
lifecycle. These field lists and transitions are not an approved storage or
public contract. In particular, reconcile the proposed server allocation of
`input_id` with the current upload API, which reserves an opaque `image_ref` and
does not currently expose an `input_id`, before designing a persisted mapping.
A1 must own and confirm task/approval lifecycle semantics.

### 8.2 Processing boundaries

Treat media processing as potentially slow and failure-prone. Video and longer
audio should be processed asynchronously; API requests should return a stable
task identity rather than hold an HTTP request open for the entire pipeline.

Persist only bounded task metadata in Cosmos: task/input IDs, household scope,
state, timestamps, attempt count, error category, and references to outputs. Do
not persist full Agent prompts, model chain-of-thought, raw media, or large
transcripts in task documents. A1 owns checkpoint/state semantics; A2 owns
recognition and media-processing providers. The queue/worker product is a later
architecture decision and is not selected by this storage document.

Suggested lifecycle for discussion:

```text
received -> processing -> awaiting_user / awaiting_approval
         -> completed / failed / cancelled
```

This is not a committed A1 protocol. Every transition must be validated and
observable. Retries reuse stable task and operation identities; a failed task
must not imply that a business mutation happened.

A2's proposed artifact fields, feature-keyword normalization, unit whitelist,
producer/version format, and retention classes also require the relevant owner
and policy agreement before becoming storage rules. An A2 reply proposed storing
complete Vision reasoning JSON as a Blob artifact; do not persist hidden
model reasoning. Store only bounded, approved extraction/summary data and
references needed by the agreed workflow. Raw media and any approved large
derived artifacts remain separate from Cosmos documents.

### 8.3 Provenance

For every confirmed business mutation, retain references to the input(s) and
derived result(s) that informed the proposal, plus the operation identity and
approval reference required by the agreed protocol. Store only facts needed to
explain the operation; do not persist hidden model reasoning. A read-only answer
or unapproved proposal must not appear as a committed inventory change.

## 9. Change events and optional Data Lake

### 9.1 Cosmos is the business source of truth

The transaction record represents an agreed inventory quantity delta. A
`change_event` is an immutable provenance/export record for a committed operation.
They serve related but different purposes and should reference each other through
stable IDs. Neither is replaced by a Data Lake copy.

Create the change event in the same transactional batch as the domain mutation.
This provides an exportable event document only for committed changes. Do not use
Cosmos Change Feed alone as an audit log: ordinary item updates can be coalesced
to the latest state, and deletes require explicit handling.

### 9.2 Lake activation criteria

ADLS Gen2 is optional and should be activated only when one or more are approved:

- Historical analytics or data-science queries exceed operational database needs.
- Long-term replay/export of immutable domain events is required.
- A formal audit/reporting requirement needs a separate analytical retention
  boundary.
- A downstream data platform has a defined consumer, access policy, and owner.

If none applies, keep events in Cosmos according to the agreed operational policy
and do not duplicate them into a lake. When activated, export only event records
and approved fields, not raw files or unrestricted Cosmos documents.

For the current forecast workload, B2 has stated in
[Issue #31](https://github.com/nestock-app/nestock/issues/31) that it has no
approved analytics consumer, lake query, long-term replay, or separate audit
retention need: forecasts and accuracy checks read Cosmos on demand, with an
estimated 700–3,000 Transactions per household per year. This workload does not
trigger Data Lake activation. Do not build a B2 lake pipeline unless an approved
consumer or other Section 9.2 criterion emerges.

### 9.3 Future export behavior

Use Cosmos Change Feed to discover immutable `change_event` records and project
them to an ADLS Gen2 append-only Bronze area. The consumer must tolerate
at-least-once delivery: use `event_id` for de-duplication, checkpoint progress,
retry transient failures, and isolate poison events for inspection/replay. Do not
claim a distributed exactly-once transaction between Cosmos and ADLS.

Suggested event envelope fields:

```text
event_id, event_type, schema_version, household_id, aggregate_type,
aggregate_id, operation_id, occurred_at_utc, source_input_ids,
approved_change_summary
```

Include actor identity only after authentication/identity semantics are approved.
Exclude secrets, SAS tokens, raw file contents, and unnecessary personal data.
Define event schema evolution and lake retention before enabling consumers.

## 10. Privacy, security, and operations

- Treat all user inputs and notes as sensitive by default.
- Encrypt in transit and at rest using Azure-managed encryption by default; assess
  customer-managed keys only if policy requires them.
- Use private access controls and least-privilege RBAC. Add private endpoints and
  network restrictions before production exposure.
- Do not log raw text, transcript, file content, access tokens, SAS URLs, or
  connection strings. Log stable correlation IDs and categorized errors.
- Do not create a user/household authorization feature as part of this B1 storage
  design. Before production access to personal data, an owner-approved identity
  and authorization design is a prerequisite.
- Keep operational and analytics access separate. Data Lake, when enabled, is
  not automatically safe for broad team access.
- Monitor Cosmos throttling/latency, storage failures, orphaned uploads, task
  age, deletion completion, and (if enabled) lake projection lag and poison events.

## 11. AI implementation contract

When an AI agent implements a task from this document:

1. Read `README.md`, `AGENTS.md`, `docs/architecture.md`, and
   `contracts/README.md` first. Confirm the user's owner code.
2. Change only paths owned by that owner. Do not modify shared contracts or
   another owner's paths without explicit approval.
3. Keep the current skeleton offline. Do not add Azure SDKs, credentials,
   deployment resources, cloud connections, or paid services unless the user
   explicitly requests an implementation phase.
4. Treat examples and proposed storage envelopes as design guidance, not public
   schemas. Pydantic domain models remain authoritative.
5. Never infer confirmation or approval from OCR, transcription, a model result,
   a client boolean, or generic JSON values.
6. Keep repositories thin. Put domain validation and business rules in shared
   domain services used by both API and Agent adapters.
7. Use explicit typed models, household-scoped methods, stable IDs, optimistic
   concurrency, and idempotency tests. Do not catch and silently ignore storage
   errors.
8. Do not persist predictions over confirmed quantity; do not store binary data,
   Base64, or prices in domain records; do not implement external checkout.
9. Add tests for household isolation, invalid scope, retry/duplicate handling,
   concurrent update conflicts, all-or-none transaction behavior, retention
   deletion, and failure/retry behavior for any implemented projection.
10. Before changing an adjustable decision, update this document and describe the
    migration, compatibility, security, and cost implications.

## 12. Phased implementation path

| Phase | Deliverable | Gate |
| --- | --- | --- |
| 0. Design | Review this proposal and settle cross-owner boundaries | No write-capable cloud connection; the approved opt-in read-only Cosmos adapter is limited to Inventory and Shopping development reads |
| 1. Local persistence boundary | Repository/service interfaces and tests against a local fake | Existing API/tool behavior remains shared and read-only until writes are approved |
| 2. Cosmos pilot | Household-scoped records, transactional inventory operation, receipt, transaction, and change event | Security, cost, recovery, and owner approvals complete |
| 3. Blob pilot | Private original-media upload, validation, metadata, retention and deletion | A2 input/processing contract agreed |
| 4. Agent processing | Async task lifecycle and derived artifact references | A1/A2 agree task and resume boundaries |
| 5. Optional Data Lake | Change Feed projection to ADLS Gen2 | Named analytics/audit consumer, retention, access, and cost approved |
| 6. Search, if needed | Search index for notes/transcripts/OCR | Query requirements and household filtering tested |
| 7. Optional member preferences | Membership-validated, per-member item preference records | Identity, membership, authorization, privacy, and preference ownership agreed; not part of Skeleton v1 |

Phases are independent gates, not a commitment to deploy every Azure service.

## 13. Decisions to revisit before production

- Cosmos region, consistency, throughput mode, autoscale/serverless choice, and
  budget alerts.
- Whether a single household partition and shared container remain suitable at
  measured scale; migration plan if not.
- Production identity, household authorization, data residency, encryption/key,
  and network requirements.
- If member preferences are implemented: stable member identity, active
  household-membership validation, member-removal behavior, preference visibility,
  and deletion/retention rules.
- Final media types, content scanning, upload limits, retention, legal holds,
  user deletion, and backup/recovery objectives.
- A1's Agent task/checkpoint/approval protocol and A2's processing/artifact
  contracts.
- Unanimous approval of the Transaction contract change and `created_at` event-time
  semantics, plus the active-link/correction rules and quantity-confirmation
  timestamp needed for late transaction resolution.
- B2's forecast algorithm and read-only backtest assumptions; any future
  persisted evaluation outcome requires a new proposal. Query behavior is
  specified in §6.6 and B1 does not pre-aggregate its input.
- C1's shopping and `complete_purchase` atomicity requirements.
- Whether Data Lake activation criteria are met, plus event schema, lake
  retention, consumers, and access ownership.
- Whether Azure AI Search is justified by actual search requirements.

When revisiting a decision, update the relevant table and add a short dated entry
with the reason, affected owners, migration impact, and rollback approach. Do not
silently convert a proposal into a production guarantee.
