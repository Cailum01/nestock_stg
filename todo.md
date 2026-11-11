# C1 todo

Reviewed: 2026-11-11 (Asia/Taipei), main 11e078f integrated into feature/c1-shopping-preview + published C1 e01ccdb + current increment.
Scope: #98, #60, #27, #90, #63. The five Issues remain open pending cross-owner integration and review. Replace this file with exactly `no todo` only when
there are no outstanding tasks.

## #98 — Media mapping / attachment retention

Source: https://github.com/nestock-app/nestock/issues/98
Dependency: PR #97 (c4e8e55), merged in main 11e078f. Container defaultTtl is tracked in
#99/#100; B1 reports it configured/verified, not an outstanding C1 deployment task.

- [x] C1: opt-in typed attachment handoff; require shown image_ref and card-bound
  mapping ETag, same household, completed state and matching internal input identity.
- [x] C1: Inventory image_ref attachment and B1 retention replace in one domain batch;
  deduplicate shared mappings and include retention in the 100-action count.
- [x] C1: synthetic cancellation, selection, retry, stale mapping, atomic conflict,
  readiness/ownership checks; isolated #97 adapter and full batch tests.
- [ ] A2/B1/C1: confirm the provisional stored-card/media handoff, producer/artifact
  metadata and application composition; wire the pipeline to the mapping port.
- [ ] B1/A2: define authoritative active attachment tracking / last-reference lookup
  before C1 implements detach or image replacement with orphan TTL. Current port
  only returns retention operations, not that proof; no new storage schema invented.
- [ ] A2/B1: verify Blob retention and cleanup separately from mapping TTL.

## #90 — New Inventory targets

Source: https://github.com/nestock-app/nestock/issues/90

- [x] C1: explicit NewInventoryTarget input and separate InventoryCreate output;
  reject linked-target overrides, fake ETags, duplicate/create-update collisions,
  invalid counts/units and missing resolved values.
- [x] C1: batch assembly for Inventory create + Shopping completion + Transaction
  + event; approval and receipt join through existing A1/B1 services in offline tests.
- [x] C1: test cancellation, collisions, atomic rollback and stable retries.
- [x] Read B1's published stable helper contract from PR #97; exercise it in the
  isolated acceptance script, without copying B1 code into the C1 working tree.
- [ ] A1/B1: integrate the actual resolver with the now-merged #97 helper; no production new-target
  wire or route is enabled by C1's test adapter.
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
- [x] C1: add inventory_quantity/unit/quantity_added outcomes for PR #96, including
  final combined quantity, new targets, unchanged removal and stored retry outcomes.
- [x] C1 test cards now use PR #95's purchased_quantity/purchased_unit/record ID shape.
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
- [x] C1: real Cosmos synthetic acceptance **18/18 passed** using one shared SDK
  container. Only household `c1-test-20261111T090129Z-c2b285db` was written;
  records retained for B1 review. Includes existing/new purchases, stable retries,
  response outcomes and actual B1 media retention helper in the same batch.
- [x] Report this C1-specific live evidence to owners in the five Issue replies.
  Production A1 resolver, real Agent/App E2E and broader live failure/reconciliation
  acceptance remain open; a successful synthetic script does not close #27.
- [ ] A1/B1/C1: choose one builder after C1 live comparison; C1 agrees to hold PR #95
  as an interim merge, following B1's recommendation. Do not substitute A1 live results.
- [x] B1 reports defaultTtl=-1 verified in #99/#100; no C1 config mutation needed.
- [ ] Attach final integrated acceptance evidence before closing #27.

## #63 — Integer count / optional Shopping Intent

Source: https://github.com/nestock-app/nestock/issues/63
Dependency: https://github.com/nestock-app/nestock/pull/64 (Draft, head 58e1900).

- [x] C1: integer arithmetic, reject fractional counts, preserve absent planned values,
  require explicit actual count and resolved unit; update preview/planner tests.
- [x] Persistent test constructs actual name-only Pydantic ShoppingItem and runs both
  existing/new-target planning. Explicit skip on old main schema; executes with #64.
- [x] C1 base changes are published as e01ccdb on the existing C1 branch.
- [ ] C1/B1: review/coordinate merge with #64 and the new increment; do not land the
  schema alone with failing C1 tests, or mark #63 closed before both are merged.

## Publication and validation

- [x] Prior C1 increment e01ccdb was committed and pushed on feature/c1-shopping-preview.
  This supersedes older Issue comments saying the branch was unpublished.
- [x] User confirmed the seven exact changed files and authorized commit/push.
- [x] User authorized a replacement PR from the same C1 branch with A1 jasonko13w review.
- [ ] A1: review the C1 PR from feature/c1-shopping-preview and coordinate production integration/merge.

Latest integrated main 11e078f + C1: **597 passed, 1 skipped** (pending #64 schema).
Backend Ruff check passed; C1 Python format and git diff --check passed.

Earlier 2026-11-11 checks (main 7b48ff2 + C1): 557 passed, 3 explicit dependency skips
(one #64 schema test and two #97 port tests). Isolated #64/#97 validation has
577 passed, no skips, including actual B1 retention helper/batch integration.
C1 lint/format and git diff --check pass.
Full format still flags two pre-existing A2-owned files: local_staging_adapter.py
and test_vision.py; they remain outside C1 scope.

User reauthorized publication; all five replies are now posted from oceanwhileru:
- #98: https://github.com/nestock-app/nestock/issues/98#issuecomment-6792632276
- #60: https://github.com/nestock-app/nestock/issues/60#issuecomment-6792632682
- #27: https://github.com/nestock-app/nestock/issues/27#issuecomment-6792633040
- #90: https://github.com/nestock-app/nestock/issues/90#issuecomment-6792633399
- #63: https://github.com/nestock-app/nestock/issues/63#issuecomment-6792633756

Auth follow-up resolved: CLI login/token acquisition succeeded with
https://cosmos.azure.com/.default. The manual acceptance script uses the same
process-local SDK scope override. No permission, role or persistent Azure config
changes were made. C1 synthetic live acceptance completed 18/18; remaining
production integration/owner decisions are still listed above.
