# Nestock Azure Data Platform Design

**Status:** B1 persistence design; records agreed boundaries and labels unresolved
decisions as proposals or future work
**Scope:** Persistence and storage boundaries for multimodal user input, inventory,
transactions, descriptive notes, and future Agent workflows
**Write boundary:** The opt-in Cosmos read path and storage primitives do not
authorize or enable business mutations. Any write workflow requires applicable
approval and must satisfy the scope, idempotency, and atomicity requirements
below.

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
| Multimodal processing | Keep original media, bounded derived observations, and business records separate | Media lifecycle and B1 mapping-storage boundaries are agreed; artifact/schema versions and A2 integration require owner coordination; long-running task persistence is outside the current MVP |
| Inventory and shopping writes | Persist the approved domain changes, transaction, operation receipt, and change event atomically within one household partition | A future bounded purchase flow requires shared read/write client composition and coordinated C2 presentation support |
| Transaction history | B2 reads raw transactions through B1's household-scoped repository; B1 does not pre-aggregate | `item_id` remains required; no feature-only transaction or transaction-item link is in the selected contract |
| Item resolution | Resolve an item before creating a Transaction; existing item aliases may be extended through an approved operation | Use layered matching; alias mutation remains part of a future approved write flow |
| Inventory removal | Soft-delete inventory records; retain IDs and transaction history | Distinct from personal-data erasure and Blob retention |
| Family member preferences | Keep household inventory shared and store `like` / `dislike` per member and item; missing preference means `normal` | Requires an approved identity/membership feature; excluded from Skeleton v1 |
| Forecast evaluation | P0 uses B2's read-only historical backtesting; no evaluation record is persisted | Any future persisted evaluation requires a new B2 proposal and authorization/read-boundary review |
| Authorization | No client-supplied value or model output authorizes a write; approval is validated server-side | Approval protocol is jointly owned with A1 and affected domain owners |
| Retention and limits | Use centralized, environment-configured pilot defaults; do not hard-code policy throughout services | Product/legal decisions can change defaults without changing the storage model |

### 1.1 Pilot defaults proposed by this document

These are conservative starting values for a future pilot, not legal retention
promises or production capacity guarantees. Keep them in one versioned
configuration/policy location and make them overrideable per environment.

| Setting | Initial default | Notes |
| --- | --- | --- |
| Unattached, abandoned upload | Delete after 24 hours | Uploads not associated with an accepted input/processing record |
| Orphaned media after processing | Delete within 24 hours | Includes media that was never attached to a confirmed business record |
| Original media attached to a confirmed business record | Retain while the attachment is active | Deleting/detaching the record must apply the documented deletion workflow |
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
  raw transaction records and computes its own aggregates; B1 provides
  household-scoped source records and query filters, not forecast-ready summaries.
- **C1** owns ShoppingService behavior and `complete_purchase`. Related purchase
  mutations must fit within one household-partition batch; storage primitives do
  not define or authorize the business workflow.
- **C2** owns frontend presentation and final wire mapping.

For image perception, A2 services return observations and must not depend on
B1's InventoryService or query inventory storage directly. A1 owns matching
those observations to inventory items; B1 provides the household-scoped
inventory reads through shared domain services and does not perform perception
or candidate selection. An observation alone never authorizes a write.

Normal API routes and Agent tools must call the same domain services. Business
rules belong in `backend/app/services`, not in routes, storage repositories, or
Agent adapters.

### Requirements for a bounded purchase flow

When implementing a bounded approval-gated purchase flow, preserve these
requirements:

- `POST /agent/messages` may persist a pending approval record, but must not
  mutate Inventory, Shopping, or Transaction records.
- `POST /agent/resume` may apply only the stored, server-validated, user-approved
  purchase values. The approval transition, operation receipts, per-operation
  Shopping and Transaction changes, change events, and any shared Inventory
  update must commit atomically in the same household partition.
- B1 requires the read and write repositories in application composition to
  share one `CosmosClient`, created through the common repository factory.
- C2 must coordinate support for rendering and handling the `pending_approval`
  response before this flow is enabled for users.
- This boundary does not authorize `create_item`, standalone stock
  adjustments, generic Agent write tools, or any unapproved value changes.

### Out of scope for Skeleton v1

- Write-capable cloud connections require an approved, bounded business workflow.
  The opt-in, Entra-authenticated Cosmos path for Inventory, Shopping, and
  forecast/TransactionHistory reads is read-only and does not authorize users
  or enable writes.
- Connecting the backend to Blob Storage, ADLS Gen2, Azure AI services, queues,
  or any other cloud service beyond that read-only Cosmos exception.
- Implementing authentication, accounts, household membership, member
  preferences, real uploads, real Agent execution, task resume, approval
  persistence, or domain mutations without the required owner decisions and
  approval boundary.
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
  ├─ Cosmos DB for NoSQL: scoped records, pending approvals, confirmed domain data
  ├─ Blob Storage: original media and large derived artifacts
  └─ Future processing adapter / queue (owner decision): OCR, transcription,
     image/video understanding, and long-running task metadata if approved

Future optional analytics path
  Cosmos append-only change-event documents
      └─ Change Feed consumer
           └─ ADLS Gen2 (Data Lake; enable only when justified)
```

### 4.1 Input-to-result lifecycle

1. Accept a text message, note, or media upload request. Assign an internal
   `input_id` only when the input is accepted for persistence. Create a `task_id`
   only if a future, explicitly approved long-running processing workflow needs
   one; the current one-turn Agent MVP does not persist separate tasks.
   Validate household scope, declared media type, actual content, configured
   limits, and request idempotency.
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
| Item aliases | Cosmos DB | Aliases on the matched `InventoryItem`; update only through a confirmed operation | None |
| Text input / descriptive note | Cosmos DB | Text, scope, timestamps, retention class, optional links | None |
| Original photo, audio, video, or document | Blob Storage | Opaque blob reference, media metadata, ownership/scope, status | Original bytes |
| Transcription, OCR, or bounded extraction | Cosmos for small structured output | Summary, typed extraction, artifact reference, provenance | Large text/artifact output when needed |
| Pending approval | Cosmos DB | Bounded server-resolved proposal, household/operation identities, approval state and lifecycle metadata | None |
| Agent task/checkpoint state | Not persisted as a separate task in the current MVP | Any future long-running task/checkpoint boundary is an A1 decision | Large intermediate artifacts may use Blob when approved |
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

Enable Cosmos TTL on this container with default TTL `-1`, so records with a
finite per-document `ttl` expire while records without one are retained.
TTL is cleanup only and must not replace authorization or approval-expiry checks.

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
| `shopping_item` | Shopping state; C1 owns service behavior | Updated only by validated domain operations; explicitly removed entries are soft-deleted |
| `transaction` | Purchase, consumption, or adjustment fact with required `item_id` and stable `operation_id` | Append-only |
| `item_alias` | No separate record; aliases are stored on the matched `inventory_item` | Updated only by a validated, confirmed operation |
| `household_membership` | Future association between a stable member identity and a household | Membership lifecycle is not implemented; identity and roles require a separate owner-approved design |
| `item_preference` | A member's preference for a shared inventory item | One current preference per member/item; omit the record for the default `normal` |
| `user_input` | Text or reference to original media | Metadata may change for processing/deletion |
| `note` | Descriptive user-provided context | Editable/deletable under explicit user action |
| `agent_task_metadata` | Not used for a separate persisted task in the current one-turn MVP | Future long-running task/checkpoint persistence is an A1 decision |
| `pending_approval` | Server-side confirmation proposal and state in the household partition | Conditional state transition; configurable 30-minute expiry |
| `operation_receipt` | Idempotency and stored operation outcome | Immutable after successful commit |
| `change_event` | Business change provenance and future export source | Append-only |
| `forecast_snapshot` | Withdrawn B2 P0 proposal for point-in-time forecast persistence | Not used in P0; B2 uses read-only historical backtesting |
| `sufficiency_check` | P1 proposal for a user's answer to a “still enough?” prompt | Out of current B1 scope; schema and write policy require B2/A1/B1 agreement |
| `forecast_evaluation` | Possible future evaluation record if a later product need requires persisted outcomes | Not used in P0; any future write requires a new B2 proposal and agreement on authorization and read-only API boundaries |

Use namespaced IDs to avoid collisions, for example
`inventory_item:<item_id>`, `transaction:<transaction_id>`,
`input:<input_id>`, `note:<note_id>`, `operation:<operation_id>`, and
`event:<event_id>`. Do not use user-provided filenames as Cosmos IDs or Blob paths.
Do not expose Cosmos `id`, partition implementation, or storage envelope as
public API fields unless owners approve a contract change.

#### New inventory item IDs

For a `create_item` operation that creates one inventory item, B1 will provide
the backend helper `resolve_inventory_item_id(operation_id: str) -> str` as the
single source of its `item_id`. The helper deterministically maps the stable
`operation_id`; retries of the same operation therefore resolve to the same ID.
It uses UUIDv5 with the standard URL namespace and the name
`nestock:inventory-item:{operation_id}`; changing this derivation would change
persisted identities and requires an explicit migration decision. Despite its
name, the helper does not look up or match existing inventory items. A1 and C1
must treat the returned ID as opaque and must not encode it themselves. Flutter
neither generates nor needs to receive it. The implementation and fixed-output
tests are in `backend/app/services/inventory_item_identity.py` and
`backend/tests/test_inventory_item_identity.py`.

Create the item only after approval, in the same household batch as the
operation receipt and other approved writes. The current scope is one new item
per operation. If one operation later creates multiple items, define and include
a stable per-item key in the resolver. This does not change the approval card
payload.

#### Pending approval storage

The MVP does not persist a separate Agent task or task-input schema. It persists
each pending confirmation as a `pending_approval` record in the existing
`household-data` container, partitioned by `household_id`. A1 owns approval
creation, lifecycle, validation, and response semantics; B1 owns the storage
primitives.

The bounded record must be sufficient to recover the exact server-resolved
proposal shown to the user: `approval_id`, household scope, card type, displayed
proposal values, each `operation_id`, purchase-option identities when applicable,
state, and creation/expiry timestamps. This is a storage boundary, not a final
public schema. Do not store prompts, model reasoning, raw media, or full
conversation history in the approval record. A1/C2 must align any client-facing
fields.

Use ETag-conditional state changes so a waiting approval cannot be consumed
twice. The approved record is in the same container and household partition as
related domain writes, allowing approval state, receipts, and domain writes to
participate in one transactional batch when the lifecycle requires it.

The offline `CosmosWriteRepository` can create a household-scoped `pending`
approval with a caller-supplied positive Cosmos `ttl`, point-read the record and
ETag, list `pending` and `confirmed` approvals through a household-scoped query
client, and make approval-only ETag-conditional replacements to `confirmed`,
`cancelled`, or `expired`. It rejects invalid transitions and conflicting reuse
of an `approval_id`. The `pending → confirmed` update does not write domain data.
The `confirmed → completed` transition remains part of the same transactional
batch as operation receipts and domain writes; it cannot be performed through
the approval-only replacement. TTL is supplied by the caller for cleanup and
does not determine whether an approval is authorized or expired.

The generic Azure SDK batch client converts create/ETag-replace operations to
Cosmos transactional batches, enforces the 100-operation limit, and maps indexed
409/412 conflicts to the repository conflict type. It logs request charge,
operation count, conflict index/status, and duration without document contents.
A read/write factory creates both repositories over one container/client.
Neither is wired into application composition, a domain service, API, or Agent
tool; this is not an approval or business-write workflow. A1 owns approval
lifecycle and validation, and no resume flow is implemented.

Confirmation expiry is 30 minutes and must remain configurable rather than
hard-coded. The server calculates the expiry and rejects confirmation after it;
the user must start a new request to receive a new confirmation. Navigation,
app close/reopen, and other client lifecycle events do not restart the timer or
cancel the approval; only explicit cancellation does. Cosmos TTL is for
eventual record cleanup, not the authorization check. Expired or cancelled
approvals do not produce transactions, and the Agent reminds the user about
pending approvals on their next message rather than relying on background
scheduling or push notifications. A1 owns the lifecycle and server-side
validation; this remains a storage contract, not an implemented approval flow.

#### Transaction identity and item resolution

Resolve a transaction to an inventory item before writing it. Keep the existing
Transaction contract: `item_id` remains required; do not add `feature_keywords`,
make `item_id` optional, or introduce `transaction_item_link` records for this
flow. A separate `recorded_at` field is not part of the current contract.

For B2 history queries, use the existing `created_at` field as the transaction
event time for filtering and ordering. The household-scoped query supports an
optional inclusive `since` and positive `limit`, ordered by `created_at`
descending and `transaction_id` ascending. It does not imply B1-side aggregation.
The exact business-time meaning and any additional timestamp needed for forecast
baseline semantics (for example, `quantity_confirmed_at`) require a separate
B2-owned contract proposal before a model or storage change.

When A1's final matching layer proposes remembering a user-provided alias, the
alias change and associated purchase must be shown in the same confirmation and
written atomically with the related operation. This is a future write capability,
not an implemented alias mutation path.

#### Operation receipts and reconciliation

Each logical operation uses its stable `operation_id`; an idempotency receipt is
created atomically with its domain effects. The B1 repository boundary must
support looking up a receipt by `(household_id, operation_id)` and reading the
related Inventory/Shopping/Transaction records. The current write primitive
checks a receipt during an idempotent retry. Domain services must also be able
to query the receipt and related records for reconciliation.

If receipt state and related domain records disagree, stop business writes,
report `reconciliation-required`, and do not auto-repair. Do not infer whether
an operation committed from current Inventory quantity alone, and do not retry
under a newly generated operation ID. A completed approval returns its stored
outcome; a confirmed approval with no receipts may retry under the lifecycle
using the same selection and operation IDs. A changed target requires fresh
confirmation.
A1/C1 own the user-facing recovery flow. Define and test the integrated path
before wiring `complete_purchase` or approval resume.

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

The offline `CosmosWriteRepository` also exposes a household-scoped
`read_operation_receipt(household_id, operation_id)` lookup that validates the
receipt identity and request fingerprint. Its offline
`execute_idempotent_batches()` primitive can create one receipt per operation ID
in the same atomic batch, include approval-level conditional writes using the ETag
from `read_pending_approval()`, and bind retries to the exact operation membership
and shared batch writes with a batch fingerprint. A partial set of existing
receipts stops with a reconciliation error instead of applying the batch. These
are storage primitives, not a recovery policy: they do not compare receipts with
Inventory, Shopping, or Transaction state. The generic Azure SDK batch client
exists but is not wired to a domain service, API, or Agent tool.

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

The current MVP does not persist separate Agent tasks; pending confirmation
records are the persistence boundary. The lifecycle is defined in
[`contracts/approval-lifecycle.md`](../contracts/approval-lifecycle.md): the
first `pending → confirmed` transition is an approval-only ETag update;
`confirmed → completed` is atomic with domain writes and operation receipts;
`cancelled` and `expired` are approval-only conditional updates; there is no
stored `failed` status. Retries reuse the same approval and operation identities.
For an unexpected partial-receipt or data mismatch, stop and report
`reconciliation-required`; do not auto-repair. Approval and successful
execution are distinct facts: a user decision alone must never be reported as
a completed write.

A1 owns rejecting invalid or cross-household approval IDs before calling B1.
Rejection performs no business write; cancelling after a committed write does
not roll it back. A failed or timed-out attempt must retain enough authoritative
state to resolve or retry using the original approval and operation identities.

#### Multi-operation confirmation batch

Cosmos transactional batches can provide all-or-nothing writes when all
operations target one container and one logical partition. The current
`household-data` container and `/household_id` partition support the agreed
confirmation group when every affected record belongs to that household. One
confirmation has one `approval_id`; each operation retains its own stable
`operation_id` and receipt. The user's approved subset is expressed by operation
identities from the displayed card; unselected operations are not written.
Modified proposal values must return to the Agent to create a fresh confirmation,
not be passed as mutable values to resume.

All writes for the approved subset must succeed or fail together in one
same-container, same-household-partition batch. This includes C1's purchase
workflow. Do not promise cross-container, cross-partition, or cross-service
atomicity. Before enabling the SDK adapter, validate the final operation count,
request size, and execution-time limits against Cosmos transactional-batch
limits; reject an oversized group before any write rather than splitting it into
partially successful writes. The batch and resume implementation remain future
work.

#### C1 purchase workflow integration

For a purchase already resolved to an existing inventory item, preserve this
boundary:

- Read the pending `ShoppingItem`, its resolved `InventoryItem`, and the current
  server-side approval state. Use only the user-reported, approved purchase
  quantity; transaction history does not infer the current purchase amount.
- Keep `ShoppingItem.quantity` as the original planned quantity. On confirmation,
  mark this purchase task `completed` even when actual purchased quantity is
  lower than planned; do not automatically retain or create a pending remainder.
  Any further purchase intent must be explicit.
- In one same-container, same-household-partition batch, update the Shopping
  status, add only the actual approved quantity to confirmed Inventory, append
  the purchase `Transaction`, and create the applicable operation receipt and
  change event.
  Reject before writing if the operation cannot fit that boundary; do not split
  it into partially successful writes.
- Keep each operation's server-issued identity and approved selection stable on
  retry. A matching receipt returns the prior outcome; a reused identity with
  different content conflicts. A timeout is not proof of failure.
- A completed item is hidden from the active Shopping List; completion is not
  hard deletion. Manual removal does not change Inventory or create/reverse a
  purchase Transaction.

For manual removal, B1 proposes a `deleted_at` metadata marker on the existing
`shopping_item` document, not a third Shopping status. Active Shopping List
reads return only `pending` items without that marker; completed items remain
distinguishable from explicitly removed items. The marker is storage metadata,
not a new public Pydantic field or status. A household-scoped conditional update
must preserve the record and its identity.

The domain behavior above is agreed, not approval to implement domain writes.
A future removal must validate household scope, server-side approval and
target version, then apply the ETag-conditional marker and necessary operation
receipt in the same household batch. It does not change Inventory, create a
purchase or Transaction, or reverse existing history.

Approval-state participation in the business batch is settled in
[`contracts/approval-lifecycle.md`](../contracts/approval-lifecycle.md). C1
also confirmed the recovery policy for an unexpected partial receipt or
receipt/domain mismatch: stop, report `reconciliation-required`, and do not
auto-repair or assign a new operation identity. These decisions do not mean
that a domain write adapter or user-facing recovery flow is implemented.

#### C2 confirmation UI integration boundary

The frontend must distinguish presentation state from approval and server
truth:

- Field edits are local drafts until the user submits a correction. A correction
  is sent to the Agent to resolve a new proposal; it is not approval and does
  not authorize a write.
- Partial selection expresses which displayed operations the user intends to
  approve. The backend must validate those identities against the stored
  proposal; the UI control and local selection state are not authorization.
- A pending card may retain its view/draft state while navigating within the
  active app session. This alone does not require restoring an approval after
  process restart; server-side approval persistence follows the boundary agreed
  in the server-side approval record.
- On an ambiguous confirmation timeout, the UI retains the proposal and retries
  with the original approval/operation identities. Only an authoritative server
  outcome can show the operation as completed; client state cannot infer whether
  a write committed.

This records C2's presentation requirements, not an authoritative wire model.
Correction request fields remain subject to A1/C2 agreement. The same-partition
atomicity and selected-operation semantics are required; server-side approval
validation and write execution remain unimplemented.

#### Item matching and alias persistence

A1 selected resolving the inventory item before writing a Transaction. The
Transaction remains linked directly by its required `item_id`; do not create
feature-only Transactions or `transaction_item_link` records under this approach.
For a new user expression that is resolved to an existing item, the confirmation
may include an explicit request to remember that expression as an item alias.
When the user approves both the purchase and alias change, persist the Inventory
alias update, purchase effects, Transaction, operation receipt(s), and change
event(s) atomically in the same household partition. Alias persistence is a
future capability and must not happen without being shown and approved.

### 6.6 Queries and indexing

Expose raw, household-scoped Transaction reads for B2. These are storage queries,
not B1-side aggregation. B2's current requirement has two query shapes:

1. **Household history:** all Transactions for a `household_id`.
2. **Item history:** Transactions for a `household_id` and exact required
   `item_id`.

Both query shapes accept optional `since` and `limit` filters:

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

For B2's history queries, `created_at` is the event-time field used for
filtering and ordering. The Transaction remains linked directly to its
required `item_id`; do not add `recorded_at`, feature-keyword transaction
queries, transaction-to-item links, or link indexes. A1 owns matching; B1
provides the exact item and household reads needed by the domain service.

Also support non-B2 household-scoped reads for listing active inventory, loading
records by stable ID, and loading notes explicitly linked to a known record.
Separate persisted Agent-task reads are outside the current MVP; input metadata
reads require an agreed input persistence workflow before implementation.

Forecast history adapters use the shared B1 `TransactionQuery` and
`CosmosReadRepository`; application composition should inject the same
repository/client used by Inventory and Shopping rather than create another
client or credential. This avoids duplicate connections and lifecycle
management while preserving B1's query boundary. This does not expose a new
public Transaction route or change the read-only scope.

#### Forecast test connectivity

When all `NESTOCK_COSMOS_*` settings are present, forecast API integration tests
use the configured Cosmos DB through the read-only data path. Do not override
the Inventory or TransactionHistory dependencies to force demo services in this
mode. Use synthetic development records, keep household
scope, and do not assume demo-only records such as `demo-rice` exist in the
configured container. Without Cosmos settings, the existing demo fallback
remains available. This decision permits read-only development access only; it
does not authorize test writes or other cloud connections.

Use the partition key and record type in queries. Add composite/range indexes only
when a concrete query and measured workload justify them. Keep indexing policy in
one reviewed configuration. The household Transaction query's
`created_at DESC, transaction_id ASC` ordering requires a Cosmos composite index
with those paths and sort directions before the query is deployed. Keep the
definition in [cosmos-indexing-policy.json](./cosmos-indexing-policy.json) and
apply/verify it on every environment's `household-data` container. The shared
development container has this index applied. No feature-keyword index is
required by the selected Transaction contract.

The proposed single `/household_id` partition is supported by B2's current
estimate of approximately 700–3,000 transactions per household per year, or
200–700 over 90 days for a household of up to four people. This is a workload
assumption, not a Cosmos quota guarantee; measure storage, RU consumption, and
hot-partition behavior before production sizing.

Cosmos is not assumed to provide general-purpose full-text or semantic search.
If users need broad search over notes, OCR, or transcripts, evaluate Azure AI
Search as a separate optional index. It must enforce household filters, support
deletion propagation, and remain a derived index rather than the source of truth.

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
quantity shown on the confirmation card. Resolve the item before writing a
Transaction; if the item or system unit cannot be identified confidently, ask a
follow-up question rather than writing an unresolved Transaction. A2's proposed
versioned unit whitelist remains subject to owner agreement; no global unit
registry or general conversion engine is implied.

#### P0 countable quantities and Shopping Intent

Persisted `InventoryItem.quantity` and `Transaction.quantity_delta` use
integers for countable inventory units. `ShoppingItem.quantity` is an optional
integer and `ShoppingItem.unit` is optional: a Shopping entry may contain only
its `name`, and omitting quantity must not silently default it to `1`. This
does not make weight or volume package specifications inventory quantities,
and it does not constrain B2's calculated forecast values to integers.

Keep the planned Shopping quantity distinct from the actual purchased quantity.
On purchase completion, only the user-confirmed actual integer quantity updates
Inventory and the purchase Transaction; the original planned quantity remains
on the completed Shopping entry. If no planned quantity was supplied, do not
invent one. Agent additions retain their HITL confirmation; a Direct Shopping
UI submission is explicit user intent but still requires backend validation and
must report success only after persistence. Both entry paths use C1's shared
ShoppingService. Stable request identity and its binding to operation IDs,
approvals, and receipts require cross-owner integration.

B2 has identified a possible need for an authoritative time marking when an
Inventory quantity was last confirmed, distinct from general `updated_at`.
`quantity_confirmed_at` or equivalent is not part of the current domain model.
Before adding it, B2 must define the forecast semantics and affected
quantity-changing workflows with B1/C1; do not infer it from `updated_at` or
introduce a persistence-only timestamp without that agreement.

When forecast inputs are insufficient, B2 returns a status and suggested next
step; it does not persist an inferred adjustment. The Agent may politely ask
whether the user wants to report the current quantity. The user can decline; if
they provide a quantity, it still requires a confirmation card before any write.
Forecast output never replaces confirmed inventory. Any reminder to report a
missed purchase occurs only while the user is using the app; do not add a
scheduler or push notification for this behavior.

#### B2 forecast evaluation (P0 uses read-only backtesting)

P0 forecasting uses read-only backtesting rather than persisted
`forecast_snapshot` or `forecast_evaluation` records. B2 evaluates its
rule-based forecast offline by replaying historical household Transactions as
of past dates and comparing computed consumption with subsequent
purchase/consumption records. This requires only the existing household-scoped,
read-only Transaction history; B1 does not add a record type, write, index, or
forecast-specific repository method for this P0 approach.

The backtest can evaluate daily consumption, but Transactions do not record the
historical confirmed inventory quantity needed to fully evaluate the date stock
ran out. If B2 later needs that outcome or a user-facing accuracy view, it must
submit a new proposal covering required data, retention, authorization, and how
any writes coexist with read-only forecast/API behavior. Do not silently restore
the withdrawn P0 proposal.

The proposed `sufficiency_check` record and “no second confirmation” answer
write are P1 and out of current B1 scope. Do not implement its schema, reads,
or writes as part of the current B1 work.

No forecast persistence schema, stable-ID policy, retention rule, or write
authorization is needed for this P0 backtest. If a later B2 requirement adds
persisted evaluation outcomes, B2 owns the schema and scoring logic; A1 and B1
must agree on authorization and read-only API boundaries before B1 adds storage
mapping, partition-scoped repository primitives, or indexes.

#### Soft-deleted inventory and shopping entries

Removing an inventory item sets a persistence-level deletion marker such as
`deleted_at`; it does not physically delete the item or its Transactions.
Default active inventory reads, including lists and point reads, omit
soft-deleted items, while authorized history can retain references to them. A
later confirmed match may restore the same `item_id`, preserving history for
forecasting.

B1 proposes representing explicit Shopping removal with the same storage-only
`deleted_at` marker on the `shopping_item` document, rather than adding a third
public Shopping status or physically deleting the record. The active Shopping
List read returns only `pending` records without a deletion marker; completed
entries remain distinguishable from explicitly removed entries. This
storage-only representation is agreed with C1. When implemented, removal must use the
household partition, validate the approval and target version, and conditionally
write the marker with its operation receipt in the same batch to avoid removing
a stale item.

Reusing an old identity does not make its old on-hand quantity current. Require a
fresh user-confirmed quantity before treating a restored item as current. Keep
soft deletion distinct from privacy-driven personal-data erasure in §7.3; an
erasure request follows a separately approved retention/legal workflow.

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

If an input is accepted for persistence, the internal record is household-scoped
and may include:

- `input_id`, `household_id`, `input_kind` (`text`, `image`, `audio`, `video`),
  `created_at`, and `retention_class`.
- Either text content or an opaque reference to the original Blob object.
- Optional `task_id`, explicit note/entity links, and processing state.
- References to derived artifacts with producer and schema/version metadata.

The exact model and API shape remain provisional. Do not add a generic “all
possible fields” model to the domain contract before the owners agree on it.

The internal persistence boundary may use `user_input` and
`derived_artifact` records and an input-processing lifecycle. Persist only
bounded, structured, objective observations; never persist hidden model
reasoning. Clients and Agent requests use opaque `image_ref`; `input_id` is an
internal persistence identity and is not exposed by the upload authorization
API. The mapping is created and validated server-side: when the backend
authorizes an upload it writes an initial mapping record (internal `input_id`,
opaque `image_ref`, `household_id`, `content_type`, expected `file_size_bytes`,
state `allocated`) instead of parsing client-supplied paths, and later use of
an `image_ref` is validated by a household-scoped server-side lookup. A2's
pipeline never accesses Cosmos directly; it calls a storage port provided by
B1. Internal record and method names are provisional, not public API schema.
Do not assume a 1:1 mapping to an internal media ID without validating
ownership.

The proposed artifact set, producer/schema versions, retention classes, and
input-record fields remain subject to the relevant owner agreement. Original
binary media belongs in private Blob Storage, never in Cosmos. Cosmos may hold
the opaque reference and the minimum approved metadata.

### 8.2 Processing boundaries

Separate media-processing status from Agent approval status. A2 proposed the
media-processing lifecycle:

```text
allocated -> received -> processing -> completed / failed
```

This is an agreed internal boundary, not a public contract:

- **Create**: `POST /api/media/upload-authorization` allocates the internal
  `input_id` and opaque `image_ref`, writes the initial mapping record as
  `allocated`, and sets an upload authorization expiry (about 300 seconds).
- **Upload/process complete**: the storage receiver marks `received` with the
  actual `file_size_bytes` and `sha256_hash`; the Vision pipeline finishing
  marks `completed` with the bounded `artifact_ref`. Only `completed`
  references are usable by the Agent (for example `verify_media_exists`);
  every other state is rejected.
- **Fail**: upload timeout, payload validation failure, and processing failure
  all transition to `failed`; the cause is kept in a separate
  `failure_reason` field (`upload_timeout`, `validation_failed`,
  `processing_failed`) so the state machine stays minimal.
- **Delete/purge**: orphaned media — never attached to any confirmed business
  record, including uploads that never finished, validation or processing
  failures, inputs that produced no confirmation card, and cards that expired
  or were cancelled — is physically cleaned up within 24 hours via Cosmos TTL
  and Blob lifecycle policy. Media attached to a confirmed business record is
  retained while the attachment is active (Section 1.1), not for a fixed
  90 days; the mapping record's retention is symmetric with its media. The
  confirmation-card 30-minute expiry is a business deadline only
  and does not define orphanhood. Deletion relies on infrastructure
  TTL/lifecycle policies; service code does not implement deletion schedulers.

`completed` means extraction/processing finished; it does not mean a business
operation was approved or written. Confirmation-card presentation, approval,
resume, and operation execution belong to A1's separate Agent lifecycle.

The current one-turn MVP does not persist separate Agent tasks or checkpoints.
Long-running video/audio work, queues/workers, and persistent task/checkpoint
metadata are future work requiring A1/A2 agreement. If later approved, persist
only bounded task metadata (scope, status, timestamps, attempt/error category,
and references), not prompts, hidden reasoning, raw media, or unbounded model
output.

Feature-keyword normalization is not used for Transaction persistence; resolve
the inventory item before writing. A2's name/quantity/unit
extraction, unit whitelist/version, artifact producer/schema version format, and
retention classes still require the relevant owner/policy agreement. Derived
artifacts must be bounded objective observations such as detected-item facts,
bounding boxes, or validated transcription output. Do not persist chain-of-
thought or complete model reasoning JSON. Keep large approved artifacts in Blob
and references/minimal metadata in Cosmos.

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

**Provisional ChangeEvent contract.** Its shape and batch placement are:

- One event per `operation_id`; `event_id` equals `operation_id` and the Cosmos
  document id is `change_event:{operation_id}`, so a retry maps to the same
  document and the operation receipt de-duplicates it.
- Fields: `household_id`, `operation_id`, `approval_id`, `transaction_id`,
  `shopping_item_id`, `item_id`, `occurred_at` (purchase time) and `recorded_at`
  (approval decision time). Timestamps are stored as UTC.
- Batch placement for a purchase: the merged inventory update goes in
  `shared_operations`; each purchased item's shopping-item update, Transaction and
  change event go in `operations_by_id[operation_id]`. Replace operations require
  `if_match_etag`, and a batch holds at most 100 operations.
- `Transaction.created_at` means the event time (`occurred_at`). B2 decides when
  time-based forecasting is enabled.
- Append-only: the event is never replaced or deleted by application code.

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

Forecasting served by household-scoped Cosmos reads does not by itself trigger
Data Lake activation. Do not build a forecast lake pipeline unless an approved
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
3. Keep the default skeleton offline when Cosmos is not configured. When all
   `NESTOCK_COSMOS_*` settings are configured, use the actual Cosmos DB through
   the approved read-only development path for Inventory, Shopping, and forecast
   API reads/tests. Use synthetic test data and preserve household scope. Do not
   add other cloud connections or write-capable cloud access without an explicit
   decision.
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
| 0. Design | Review this proposal and settle cross-owner boundaries | No write-capable cloud connection; the approved opt-in read-only Cosmos path covers Inventory, Shopping, and configured Forecast development reads |
| 1. Local persistence boundary | Repository/service interfaces and tests against a local fake | Existing API/tool behavior remains shared and read-only until writes are approved |
| 2. Cosmos pilot | Household-scoped records, transactional inventory operation, receipt, transaction, and change event | Security, cost, recovery, and owner approvals complete |
| 3. Blob pilot | Private original-media upload, validation, metadata, retention and deletion | A2 input/processing contract agreed |
| 4. Agent processing | Future long-running processing and derived artifact references | A1/A2 agree task, processing, and resume boundaries; not part of current one-turn MVP |
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
- A1's approval lifecycle and any future long-running task/checkpoint protocol;
  A2's input/artifact mapping and processing contracts.
- Any new B2 proposal for authoritative `quantity_confirmed_at` semantics or
  additional Transaction timestamps; the selected Transaction contract keeps
  `item_id` required and does not use feature-only transactions or link records.
- B2's forecast algorithm and read-only backtest assumptions; any future
  persisted evaluation outcome requires a new proposal. Query behavior is
  specified in §6.6 and B1 does not pre-aggregate its input.
- B1/C1/A1's approval-state/write consistency and unexpected receipt-mismatch
  recovery; C1's manual Shopping removal persistence representation.
- Whether Data Lake activation criteria are met, plus event schema, lake
  retention, consumers, and access ownership.
- Whether Azure AI Search is justified by actual search requirements.

When revisiting a decision, update the relevant table and add a short dated entry
with the reason, affected owners, migration impact, and rollback approach. Do not
silently convert a proposal into a production guarantee.
