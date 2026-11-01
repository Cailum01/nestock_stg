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
        -- Agent API --> Agent / Tools -+--> Shared domain services --> Cosmos (planned)
```

The current frontend uses injected offline mocks. Backend GET routes and query
tools delegate to the same read service instances, replaceable through dependency
injection. There are no write endpoints or mutation ports. Agent routes validate
requests but return HTTP 501. No cloud SDK or LangGraph dependency is installed.
LangGraph remains the agreed runtime baseline for A1's later work; Azure OpenAI and
Cosmos DB for NoSQL remain planned providers.

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
binary data does not belong in inventory. Storage provider is unselected.

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
| A2 | Vision, item normalization, image processing/storage integration, commerce provider, purchase-option retrieval |
| B1 | Cosmos containers, partition keys, indexing, document mapping, atomic writes/idempotency, append-only transaction-to-item links, soft deletion, and system-owned canonical units |
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
- Demo fixtures are not persistent or forecasted facts; UI mocks do not integrate
  with HTTP. Two demo GET routes expose the shared-service boundary for development.
- Domain numbers use finite Python floats; precision/unit policy remains undecided.
- Mutations are deliberately uncallable until owners agree on enforceable approval
  and persistence semantics. This is not a completed HITL implementation.
