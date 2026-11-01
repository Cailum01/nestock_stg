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
decisions for them.

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

- Connecting to Cosmos DB, Blob Storage, ADLS Gen2, Azure AI services, queues, or
  any other cloud service.
- Implementing authentication, accounts, household membership, real uploads,
  real Agent execution, task resume, approval persistence, or domain mutations.
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
| `transaction_item_link` | Approved attribution of a previously feature-only transaction to an item | Append-only |
| `user_input` | Text or reference to original media | Metadata may change for processing/deletion |
| `note` | Descriptive user-provided context | Editable/deletable under explicit user action |
| `agent_task_metadata` | Bounded task status and input/artifact references | State transitions validated; A1 owns runtime semantics |
| `operation_receipt` | Idempotency and stored operation outcome | Immutable after successful commit |
| `change_event` | Business change provenance and future export source | Append-only |
| `forecast_evaluation` | Internal B2 prediction and later outcome for P0 accuracy evaluation | Append-only prediction/evaluation snapshots; B2 owns schema |

Use namespaced IDs to avoid collisions, for example
`inventory:<item_id>`, `transaction:<transaction_id>`,
`input:<input_id>`, `note:<note_id>`, `operation:<operation_id>`, and
`event:<event_id>`. Do not use user-provided filenames as Cosmos IDs or Blob paths.
Do not expose Cosmos `id`, partition implementation, or storage envelope as
public API fields unless owners approve a contract change.

#### Proposed Transaction contract change (pending unanimous approval)

The current model requires `item_id`. B2 proposes making `item_id` optional and
adding a `feature_keywords` snapshot. Validation must require at least one of:

- a non-empty `item_id`; or
- a non-empty `feature_keywords` snapshot.

Both may be present. The snapshot contains the exact normalized feature terms
known when the transaction was recorded; it is immutable with the Transaction.
For example, an unlinked soy-sauce purchase can be recorded with a normalized
`醬油` snapshot and later resolved to a specific item. This is a proposed shared
domain/API contract change, not approved by this B1 storage document. Do not
change the Pydantic model, OpenAPI contract, or existing contract documentation
until all owners approve the schema and migration.

For B2's date-window queries and late-link reconciliation, define Transaction
`created_at` as the UTC business occurrence time (for example, the purchase
date), including when a user reports it later. Persist a separate storage-only
`recorded_at` timestamp for when the system accepted the record. B2 filters and
orders on `created_at`; reconciliation compares `created_at` with the item's
quantity-confirmation timestamp. This semantic change also requires unanimous
approval; do not silently reinterpret existing records.

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

### 6.4 Atomic inventory writes

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

Transaction signs, unit conversions, and quantity arithmetic policy remain
domain decisions; persistence must store the already validated domain operation,
not invent those rules.

Shopping operations and `complete_purchase` need C1's decision on which records
belong in the same atomic batch. Do not claim cross-container, cross-partition,
or cross-service atomicity.

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

The existing domain model has `updated_at`, but that timestamp may change for
non-quantity edits. The persistence design therefore needs an authoritative
quantity-confirmation timestamp. Prefer an explicit `quantity_confirmed_at`
field or equivalent persistence metadata; do not use general `updated_at` unless
the contract guarantees it changes only when quantity is confirmed. Agree any
public model change before exposing it through Pydantic/OpenAPI.

### 6.5 Queries and indexing

Expose raw, household-scoped Transaction reads for B2. These are storage queries,
not B1-side aggregation:

1. **Household history:** all Transactions for a `household_id`.
2. **Item history:** Transactions for a `household_id` and exact `item_id`,
   including feature-only transactions whose active append-only link resolves
   them to that item. Return each `transaction_id` once; do not rewrite the
   original transaction to populate its item.
3. **Feature history:** Transactions for a `household_id` and an exact normalized
   feature keyword in the transaction's keyword snapshot. This returns both
   already item-linked and unmatched transactions so the Agent can offer relevant
   candidates and ask the user to resolve ambiguity.

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

For attribution, B2 reads the matching `transaction_item_link` records along
with Transactions; it does not read `change_event`. The link records are
association metadata, not extra quantity transactions. B1 may expose a raw
transaction query resolved through these links, but does not calculate
consumption, aggregate history, or materialize B2's prediction input.

Also support non-B2 household-scoped reads for listing active inventory, loading
records by stable ID, loading notes explicitly linked to a known record, and
loading task/input metadata by stable task or input ID.

Use the partition key and record type in queries. Add composite/range indexes only
when a concrete query and measured workload justify them. Keep indexing policy in
one reviewed configuration. Transaction queries need a composite index compatible
with `created_at DESC, transaction_id ASC`; feature-keyword equality plus the
requested sort may require an additional composite index, to be verified against
the selected Cosmos SDK and query plan.

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

### 6.6 Units, soft deletion, and forecast evaluation

#### System-defined units

The system owns each inventory item's canonical unit. API and persistence
validation reject a client attempt to set or change it. Transaction has no
`unit` field; its quantity delta is expressed in the associated item's canonical
unit. Before asking for approval, the Agent resolves package descriptions (for
example, one six-bottle carton) to an exact canonical quantity (six bottles) and
shows that quantity to the user.

A feature-only Transaction can be stored only when the system can deterministically
resolve its normalized feature to a versioned, system-owned unit definition. Keep
the definition version as persistence metadata, not as a user-settable unit or a
new `Transaction.unit` field. If the canonical quantity/unit cannot be resolved
unambiguously, ask a follow-up question and do not write an untyped quantity.
No general unit-conversion engine is implied.

#### Soft-deleted inventory

Removing an item sets a persistence-level deletion marker such as `deleted_at`;
it does not physically delete the item or its Transactions/links. Default
inventory lists omit soft-deleted items, while authorized history and feature
resolution can still find them. A later confirmed match may restore the same
`item_id`, preserving history for forecasting.

Reusing an old identity does not make its old on-hand quantity current. Require a
fresh user-confirmed quantity or apply only a later, approved, not-yet-counted
Transaction under the §6.4 rule. Keep soft deletion distinct from privacy-driven
personal-data erasure in §7.3; an erasure request follows a separately approved
retention/legal workflow.

#### Forecast evaluation records

For P0, B2 may append an internal `forecast_evaluation` record to Cosmos for
measuring prediction accuracy. Store a stable evaluation ID, household/item
reference, prediction timestamp and horizon/result, algorithm version, relevant
input window or source Transaction IDs, and a later observed outcome when
available. Do not expose these records in user-facing API responses or treat
predictions as confirmed stock. Apply the domain-record retention policy rather
than the short raw-media TTL. B2 owns the evaluation schema and scoring logic;
B1 provides household-scoped persistence only.

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
| 0. Design | Review this proposal and settle cross-owner boundaries | No cloud connection |
| 1. Local persistence boundary | Repository/service interfaces and tests against a local fake | Existing API/tool behavior remains shared and read-only until writes are approved |
| 2. Cosmos pilot | Household-scoped records, transactional inventory operation, receipt, transaction, and change event | Security, cost, recovery, and owner approvals complete |
| 3. Blob pilot | Private original-media upload, validation, metadata, retention and deletion | A2 input/processing contract agreed |
| 4. Agent processing | Async task lifecycle and derived artifact references | A1/A2 agree task and resume boundaries |
| 5. Optional Data Lake | Change Feed projection to ADLS Gen2 | Named analytics/audit consumer, retention, access, and cost approved |
| 6. Search, if needed | Search index for notes/transcripts/OCR | Query requirements and household filtering tested |

Phases are independent gates, not a commitment to deploy every Azure service.

## 13. Decisions to revisit before production

- Cosmos region, consistency, throughput mode, autoscale/serverless choice, and
  budget alerts.
- Whether a single household partition and shared container remain suitable at
  measured scale; migration plan if not.
- Production identity, household authorization, data residency, encryption/key,
  and network requirements.
- Final media types, content scanning, upload limits, retention, legal holds,
  user deletion, and backup/recovery objectives.
- A1's Agent task/checkpoint/approval protocol and A2's processing/artifact
  contracts.
- Unanimous approval of the Transaction contract change and `created_at` event-time
  semantics, plus the active-link/correction rules and quantity-confirmation
  timestamp needed for late transaction resolution.
- B2's forecast algorithm and evaluation-record schema; query behavior is
  specified in §6.5 and B1 does not pre-aggregate its input.
- C1's shopping and `complete_purchase` atomicity requirements.
- Whether Data Lake activation criteria are met, plus event schema, lake
  retention, consumers, and access ownership.
- Whether Azure AI Search is justified by actual search requirements.

When revisiting a decision, update the relevant table and add a short dated entry
with the reason, affected owners, migration impact, and rollback approach. Do not
silently convert a proposal into a production guarantee.
