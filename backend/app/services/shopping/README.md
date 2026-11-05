# C1 shopping workflow preview

This is an offline, pure business-rule prototype. It is not wired to routes,
Agent tools, dependency composition, Cosmos, or approval/resume. It performs no
writes and does not implement `complete_purchase` execution.

Related coordination: [Issue #27](https://github.com/nestock-app/nestock/issues/27)
and [PR #4](https://github.com/nestock-app/nestock/pull/4). This preview does not
close the cross-owner consistency and execution questions in Issue #27.

## Supported previews

- Partial purchase: show actual purchased quantity, remaining pending quantity,
  and the proposed resulting stock for an already resolved inventory target.
- Full purchase: propose `completed` with zero remaining quantity. C2 should hide
  completed entries from the pending view after the final contract is agreed.
- Manual removal: describe only removal of the pending shopping entry; it does
  not imply an inventory adjustment or a purchase/reversal transaction.

Inputs are snapshots of the existing authoritative Pydantic models. Outputs are
internal dataclasses, not HTTP schemas, approval evidence, or database documents.
The prototype treats the input shopping quantity as outstanding demand, per C1's
requested behavior. This does not change the shared model's semantics: B1/C2 must
agree its persisted representation (including completed quantity) before wiring.

## Integration gates

- A1: validate server-stored approval, bind every relevant target/value, assign
  operation identity before approval, and reuse it on retries. Previewing does
  not verify user provenance, approval, expiry or concurrent snapshot freshness.
- B1: atomically persist Shopping, applicable Inventory, purchase Transaction,
  receipts and events. Preserve snapshot versions; stale values require a fresh
  proposal/approval, not silent recalculation. No fake atomicity or durable
  idempotency is claimed by this module.
- Multi-item writes are **provisional per D-3**. The per-item operation/receipt
  representation and group interface must be aligned with A1/B1 before execution.
  Do not split an oversized group into partially successful writes.
  If only some receipts exist, stop without supplementing writes. A1/B1 must
  define reconciliation of already-applied effects before a new approval can
  safely execute; assigning new IDs alone must not duplicate those effects.
- C2: coordinate actual-purchase input, pending remainder, completed hiding and
  manual removal. Removal storage semantics remain a B1/C1 integration decision.
- Feature-only/unlinked purchases, new inventory targets, unit conversion,
  purchases above outstanding demand, and historical purchase reconciliation
  remain explicit gaps. `ShoppingDecisionRequired` signals an unsupported preview,
  not a new product prohibition. Do not infer a solution or persist a transaction.

No operation/transaction IDs, timestamps, database envelopes or approval booleans
are invented here. Decimal arithmetic avoids float subtraction artifacts locally;
the shared precision/unit policy is still open.

Run from `backend/`: `uv run --locked pytest tests/test_shopping_preview.py`.
