# C1 shopping previews and domain plans

These services are offline and perform no I/O. They are not wired to routes,
Agent tools, dependencies, approval/resume, or Cosmos. A plan is not authorization
and does not mean that `complete_purchase` has committed.

## Agreed behavior

C1 accepted C2/B1's quantity/completion proposal on 2026-11-04 in
[Issue #27](https://github.com/nestock-app/nestock/issues/27#issuecomment-6632541140):

- Keep `ShoppingItem.quantity` as the original planned quantity.
- Planning two bottles and buying one means Inventory +1, one purchase Transaction
  for the actual quantity, and Shopping completed with planned quantity still two.
- Do not retain or create an automatic pending remainder. Another shopping intent
  requires an explicit user request.
- Completed items disappear from the active list; they are not hard-deleted.

C1 and B1 also aligned in Issue #27 on 2026-11-06:

- Manual removal targets a valid pending Shopping entry using a storage-only
  `deleted_at` tombstone, preserving public pending/completed status. It does not
  update Inventory or create/reverse a Transaction.
- Follow [A1's lifecycle](../../../../contracts/approval-lifecycle.md): first
  confirmation conditionally stores confirmed and fixes selection; execution
  atomically commits confirmed -> completed, domain changes and receipts.
- Partial receipts or receipt/domain inconsistencies stop execution and require
  reconciliation. MVP does not automatically repair or replay with fresh IDs.

The decision index still labels D-3 pending, whereas the lifecycle and B1 design
record the Issue #42 agreement. Retain **provisional per D-3** in integration PRs
until the index is synchronized by its owner; do not independently change it here.

## Available C1 services

`ShoppingPreviewService` describes a single purchase or removal using existing
Pydantic snapshots. It performs local Decimal arithmetic without changing source
records. It does not establish provenance, freshness, approval or persistence.

`ShoppingPlanService.plan_purchase` takes only the selected, server-resolved
`PurchaseInput` records and a stable timezone-aware `recorded_at` supplied by the
proposal owner. It returns a single `PurchasePlan` containing:

- Shopping replacements retaining planned quantities and stable operation IDs.
- One Inventory replacement per distinct target with its original ETag and all
  contributing operation IDs. Multiple selected shopping entries for one target
  are summed against one consistent snapshot, avoiding lost increments.
- One purchase Transaction per operation with actual quantity, caller-provided
  stable transaction identity and timestamp; no IDs or times are generated.

Every member is validated before any plan is returned. Duplicate operation,
transaction or Shopping IDs, inconsistent snapshots/ETags for a shared target,
missing versions, cross-household records and nonfinite combined stock fail the
entire plan. No partial result or source mutation occurs. Rebuilding from the
same inputs is deterministic; this is not durable retry handling. The caller
must not rebuild a retry using newly read stock or a fresh timestamp.

`plan_removal` returns only the pending Shopping identity, household, operation
identity, expected ETag and proposed deletion timestamp. It does not construct a
Cosmos document or expose `deleted_at` in the public ShoppingItem model.

Both services currently support only purchases already linked to existing
inventory, in the same resolved canonical unit. Unlinked/new targets, unit
conversion, purchases exceeding planned demand and historical reconciliation
remain explicit `ShoppingDecisionRequired` gaps, not product prohibitions.
The current shared quantity contract remains float; no integer migration or
precision policy is introduced by these services.

## Concrete integration blockers

1. **A1: approved typed inputs and execution handoff.** Main currently has no
   approval service or typed purchase executor port. Agree how the exact stored
   proposal, fixed selection, source ETags, stable transaction IDs/timestamps and
   completed outcome reach C1. Never execute generic operation values or accept a
   client boolean as approval. Coordinate with PR #55; this checkout contains no
   approval service, and this module does not assume that PR is integrated.
2. **B1: domain batch mapping.** `CosmosWriteRepository` has grouped receipts,
   shared writes and ETag conditions, but it is not a complete purchase adapter.
   Agree how shared Inventory updates, per-operation Transactions/Shopping,
   approval completion/outcomes and required change events form one batch.
   Confirm the event contract and preserve storage metadata/tombstones when
   replacing documents. C1 deliberately does not invent these envelopes.
3. **A1/B1: recovery integration.** Read approval and all receipts after unknown
   outcomes; return stored outcomes for normal completed operations. Confirmed
   with no receipts may retry with the original identities/selection and valid
   ETags per lifecycle. Some receipts, receipts without completed, or discovered
   domain inconsistencies stop with reconciliation-required. Confirmed alone is
   not an inconsistency. No automatic repair or ID replacement is allowed.
4. **B1/C1/C2/A1: name-only inputs (Issue #52).** C1 supports optional planned
   quantity/unit, without fake defaults. Main's ShoppingItem still requires both;
   B1 owns that schema change. The existing preview/plan preconditions need
   adaptation once it lands. Direct UI approval and stable request-to-operation
   identity binding remain to align. No `add_intent` write is implemented here.
   Integer/count semantics separately await B2/B1 agreement.

The future executor must reject cross-container/partition or oversized requests
before writing, including receipts, shared writes and events in the final batch
limit (100 operations); never split the group. It must validate approval and
source ETags at persistence time. A pure plan cannot check concurrent versions,
ensure durable idempotency, authorize selection, or prove all-or-nothing writes.

## Verification and remaining acceptance

Run from `backend/`:

```sh
uv run --locked pytest tests/test_shopping_preview.py tests/test_shopping_planning.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

C1 unit tests cover actual/planned semantics, combined inventory increments,
stable identities, selected inputs, invalid groups, source preservation and
removal tombstone planning. The existing repository tests cover B1 primitives;
they are not evidence of a connected C1 execution path.

Before closing Issue #27, C1/A1/B1 must test the integrated complete_purchase and
removal path: successful commit, partial purchase, cross-household rejection,
ETag conflict, lost response/retry with no duplicate effects, receipt/data
inconsistency stopping writes, and atomic batch failure. SDK/live Cosmos writes
are not validated by this module. No claim of completed integration is made.
