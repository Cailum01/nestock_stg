# Skeleton v1 boundaries and decisions

## Inspection and implementation plan

The requested skeleton has no internal product contradictions. Planned providers
and runtime do not require live implementations in this phase. Provisional
contracts can coexist with a single authoritative backend model source; provisional
means changeable, not independently redefined by every owner.

Create the Android/iOS Flutter shell, FastAPI health route, agreed domain models,
read service/tool boundaries, minimal Agent ports, mock presentation, baseline
tests and contributor documentation. Validate tools, tests and actual launch.

## Fixed architecture

```text
Flutter -- Normal API ------------------+
        -- Agent API --> Agent / Tools -+--> Shared domain services --> Cosmos DB
```

The frontend calls the backend over HTTP by default; its injected offline mocks are
only used with `AGENT_TRANSPORT=mock`. Backend GET routes and query
tools delegate to the same read service instances, replaceable through dependency
injection. The only write path is a confirmation card: with Cosmos configured,
`POST /agent/messages` may store a pending purchase card (v8 flow C) and
`POST /agent/resume` writes it after the user confirms, through B1's Cosmos
repository. `POST /agent/messages` runs a LangGraph Agent on Azure OpenAI when
`backend/.env` is configured and returns HTTP 501 otherwise; `POST /agent/resume`
returns 503 without Cosmos. Cosmos DB
for NoSQL is provisioned on Azure. The backend can optionally use an Entra-
authenticated, read-only Cosmos adapter for Inventory and Shopping when all
`NESTOCK_COSMOS_*` settings are supplied; without them it continues to serve
read-only demo data. The adapter is not authorization, exposes no transaction
history, and performs no writes.

Pydantic models define the domain and provisional HTTP envelopes. FastAPI derives
OpenAPI for models exposed through routes. Transaction currently has no public
history route, so it does not need to appear in OpenAPI yet; use its Pydantic model
directly. There is no separately maintained JSON Schema file. Dart contains only
temporary display projections, with no wire serializer or claim of authority.

## Inventory, images and transactions

Inventory quantity is the latest confirmed value. Forecast output is separate
information; it cannot silently decrement quantity. Future changes must follow:

```text
history -> estimate -> ask user -> actual quantity -> resolved proposal
        -> HITL approval -> inventory mutation + transaction
```

Read models are frozen Pydantic objects and reject unagreed extra fields and
nonfinite numbers. This is a development guard, not a persistence authorization
system. Future service code must enforce the invariant regardless of model type.
No sign or unit-conversion policy has been invented for quantities.

Location remains free text suitable for future keyword matching. There is no
category, Location or Box model. `image_ref` is an opaque external reference;
binary data does not belong in inventory. Photo storage is tracked in Issue #98.

Transactions record purchase (acquired/restocked), consumption (explicit user
report), or adjustment (confirmed actual quantity differs). B1 persists them,
B2 reads them, and C1 workflows may create them. This does not choose a container,
partition, history aggregation strategy, or atomic write strategy.

## Provisional interfaces and safety limits

`AgentRuntime`, read service protocols, query tool adapters, six Agent/HITL models,
`AgentClient`, `CatalogClient`, Dart display projections and route presentation are
replaceable scaffolding. They do not settle final operation types, checkpoint
tokens, conversation IDs, selected-option payloads, pagination or streaming.

The six envelopes support the agreed result and approval presentation names.
Generic result data/operation values are illustrative; they are not executable
commands. Resume refers to an approval identity, not mutable client-supplied write
values. Future backend code must bind approval to stored resolved operations,
household and exact values, and handle durable retries. Those mechanisms are absent.

Read/normalize/plan before approval; resolve targets before pausing; group related
writes; obtain fresh approval for any changed relevant value; reuse operation IDs
on retries; validate in the backend. The UI preserves approval identity on retry,
but this alone provides no backend idempotency guarantee. Mock confirmation writes
nothing. The backend cannot accidentally accept it as a completed mutation.

Purchase option results may carry current prices, but ShoppingItem must not.
Selection and approval add items to the Shopping List; no external ordering/payment.
Batch and purchase-option presentations have named extension points, not complete
selection or batch workflow implementations.

## All intentionally unresolved owner decisions

| Owner | Left unimplemented |
| --- | --- |
| A1 | LangGraph graph/state, tool registry internals, checkpoint storage, interrupt/resume, final Agent/HITL wire protocol, binding/validation of approvals |
| A2 | Item normalization and image storage integration (Issue #98). Vision (Azure OpenAI), speech (Azure AI Speech) and purchase-option retrieval (BigGo) are implemented |
| B1 | Indexing, document mapping, atomic writes/idempotency, append-only transaction-to-item links, soft deletion, and system-owned canonical units. Containers and partition keys are set in `datalake/azure-data-platform-design.md` (one `household-data` container, `/household_id`) |
| B2 | Forecast/consumption algorithms, reorder thresholds, refresh strategy, raw-transaction query use, transaction-history aggregation, and forecast-evaluation records; B1 does not pre-aggregate B2 inputs |
| C1 | ShoppingService internals, complete_purchase orchestration, cross-domain persistence |
| C2 | Final scene design, responsive hotspots, navigation details, page vs overlay vs sheet, animation, final view-model and API mapping |
| Cross-owner | Proposed Transaction contract change (optional `item_id` plus `feature_keywords`, requiring at least one), exact feature normalization/query semantics, B2 use of append-only transaction-to-item links, quantity-confirmation timestamp semantics, and `complete_purchase` atomicity; typed operation values and purchase-option selection contract |

## Explicit skeleton assumptions

- Fixed `demo-household` scope, no household/auth feature.
- Traditional Chinese demo copy and display name `Nestock｜窩有`.
- Flutter-generated `com.example.nestock` identifiers are development placeholders.
- Android/iOS only; native SDK default versions are inherited from Flutter.
- Python 3.14 and Flutter 3.47.7/Dart 3.13.5 match the available local tools.
- Minimal standard Navigator page pushes are temporary C2 presentation choices.
- Demo fixtures are not persistent or forecasted facts; they serve only the offline
  fallback and automated tests. The App reads the backend over HTTP by default.
- Domain numbers use finite Python floats; precision/unit policy remains undecided.
- Mutations are deliberately uncallable until owners agree on enforceable approval
  and persistence semantics. This is not a completed HITL implementation.
