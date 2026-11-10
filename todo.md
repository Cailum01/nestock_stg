# C1 todo

Reviewed: 2026-11-10 (Asia/Taipei), main 7b48ff2 plus local C1 changes.
Scope: #90, #60, #27, #63. The four Issues remain open pending publication and
cross-owner integration. Replace this file with exactly `no todo` only when
there are no outstanding tasks.

## #90 — New Inventory targets

Source: https://github.com/nestock-app/nestock/issues/90

- [x] C1: explicit NewInventoryTarget input and separate InventoryCreate output;
  reject linked-target overrides, fake ETags, duplicate/create-update collisions,
  invalid counts/units and missing resolved values.
- [x] C1: batch assembly for Inventory create + Shopping completion + Transaction
  + event; approval and receipt join through existing A1/B1 services in offline tests.
- [x] C1: test cancellation, collisions, atomic rollback and stable retries.
- [ ] A1/B1: connect the typed stored-proposal resolver and B1's stable operation-to-item
  ID implementation; confirm exact binding before enabling a real write path.
- [ ] A1/C2: connect existing-match / new-item card lifecycle and actual App flow.
- [ ] A1/B1: settle event scope for gifts/add_stock without Shopping; C1's purchase
  builder deliberately does not invent Shopping IDs for these cases.

## #60 — Purchase-plan / batch handoff

Source: https://github.com/nestock-app/nestock/issues/60

- [x] C1: time split (event time vs first decided_at), original IDs and target ETags.
- [x] C1: ShoppingWriteBuilder + ShoppingInputResolver port; fixed selection validation,
  household/source/ETag checks, storage metadata preservation, canonical card ordering.
- [x] C1: per-operation Shopping/Transaction/events/new Inventory; shared merged existing
  Inventory; batch limit includes receipts and final completed approval.
- [ ] A1: implement/validate the trusted resolver over server-stored action/values and
  connect it to this builder; use original IDs/times, reject client edits.
- [ ] A1: split oversized intents at card generation (save-time rejection is tested).
- [ ] B1/A1: finalize optional shopping_item_id / event scope for non-Shopping writes.
- [ ] B2: accept event-time semantics and decide forecast flag enablement.

## #27 — Purchase/removal acceptance

Source: https://github.com/nestock-app/nestock/issues/27

- [x] C1: preserve planned quantities, apply actual counts, complete without remainder.
- [x] C1: conditional removal with deleted_at, no Inventory/Transaction changes.
- [x] Offline integrated tests use actual C1 planner/builder + A1 ApprovalService +
  B1 CosmosWriteRepository. Container and production A1 resolver are stand-ins.
- [x] Offline tests: success, partial purchase/selection, new item, removal, cancel,
  household rejection, stale versions, race during commit, create conflict, atomic
  rollback, timeout before/after commit, fixed time/ID retry, partial receipts stop,
  and 100-action acceptance / oversized-card rejection.
- [ ] A1/C1/B1: complete real Agent/App stored-proposal-to-resolver-to-builder wiring;
  validate real action payload binding and known domain inconsistency handling.
- [ ] B1: validate live Cosmos, including container TTL configuration. No cloud writes
  were performed by this increment.
- [ ] Attach final integrated acceptance evidence before closing #27.

## #63 — Integer count / optional Shopping Intent

Source: https://github.com/nestock-app/nestock/issues/63
Dependency: https://github.com/nestock-app/nestock/pull/64 (Draft, head 58e1900).

- [x] C1: integer arithmetic, reject fractional counts, preserve absent planned values,
  require explicit actual count and resolved unit; update preview/planner tests.
- [x] Persistent test constructs actual name-only Pydantic ShoppingItem and runs both
  existing/new-target planning. Explicit skip on old main schema; executes with #64.
- [ ] C1/B1: publish/review C1 changes and coordinate merge with #64; do not land the
  schema alone with failing C1 tests, or mark #63 closed before both are merged.

## Publication and validation

- [ ] User: confirm the exact changed files before commit (AGENTS.md requirement).
- [ ] C1: commit with user's identity and push the existing C1 branch after confirmation.
  Recheck latest main before pushing. No replacement branch or PR is authorized yet.

Current backend: 544 passed, 1 skipped (real name-only schema awaits #64).
Joint validation with #64: 548 passed, no skips, including the real name-only model
test for existing and new targets. C1 lint/format and git diff --check pass.
Full Ruff check passes; full format check flags two pre-existing A2-owned files:
local_staging_adapter.py and test_vision.py. They remain outside C1's scope.
