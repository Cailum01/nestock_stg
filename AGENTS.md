# Nestock contributor instructions

## Session start (read first)

1. The user states their owner code (A1–C2). If not, ask first. Never guess.
2. Only modify paths owned by that code (README "Where each owner extends").
   Before touching shared files or another owner's paths, stop, explain why,
   and wait for approval.
3. Git workflow (run it for the user):
   - Before creating a branch, switch to `main` and pull the latest changes.
   - Branch: `feature/<code>-<topic>`, e.g. `feature/b1-cosmos-spike`.
   - Stage only changed files by path; never `git add .`.
   - List the files to the user and wait for confirmation before committing.
   - Commit with the user's own Git identity, not as the AI.
   - Before pushing, pull the latest `main` into your branch and resolve conflicts locally.
   - Push the branch and open a pull request to `main`. Never push to `main`.
   - When opening a pull request, request a reviewer from the owner table.
     For shared files, request `jasonko13w` or `Cailum01`.
   - Never commit `.env`, keys or local environments.
4. Planned locations — create only when first needed; any other new
   top-level folder needs team agreement:
   - `prompts/` Agent and vision prompts
   - `evals/` golden prompts, workflow acceptance, vision test set
   - `mocks/` mock tool responses shared by frontend and backend
   - `data/` seed transaction history, demo price fallback
   - `datalake/` Data Lake practice track (B1)
   - `deploy/` Azure deployment notes

This repository started as a clean Skeleton v1 for **Nestock｜窩有** and is now
being built into the P0 MVP. Implement only the confirmed P0 scope. Keep the
six-owner foundation small and independently usable.

## Architecture and authoritative contracts

- Flutter Android/iOS application: `frontend/`. FastAPI application: `backend/`.
- Normal API routes and Agent tools MUST delegate to the same domain services.
  Put business rules in `backend/app/services`, never duplicate them in adapters.
- Pydantic models in `backend/app/models` are the contract source of truth.
  FastAPI generates OpenAPI for exposed routes. `contracts/` contains discussion,
  examples and decisions, not a second manually maintained schema.
- Agent/HITL models, service ports and Dart view projections are PROVISIONAL.
  Coordinate changes with affected owners; do not freeze a final protocol here.
- Cosmos DB for NoSQL, Azure OpenAI, Azure AI Speech and BigGo are in use, with
  LangGraph for the Agent. Develop and test against these real services; demo data
  is only the offline fallback and the fixture for automated tests. The A1 Agent
  (`POST /agent/messages`) connects to Azure OpenAI only when `backend/.env` is
  configured, and returns 501 otherwise. Agent writes go only through approval
  cards (HITL rule 8).

## Mandatory product invariants

- Predicted inventory must never overwrite confirmed `InventoryItem.quantity`.
  Quantity changes require user-provided information and applicable HITL approval.
  Forecasting may estimate, flag, recommend checking, and ask for actual quantity.
- Inventory has no category. Location is human-readable, keyword-search-compatible
  free text; no Location or Box entity. `image_ref` is opaque external metadata,
  never binary/base64. Photo recognition uses Azure OpenAI; photo storage is
  tracked in Issue #98.
- Shopping status starts with pending/completed. Never persist product price.
  Source/product URL may be retained. Purchase options are current recommendations;
  approval adds selected options to the Shopping List, never orders or pays.
- Transactions have stable operation identity and purchase/consumption/adjustment
  reasons. B1 persists, B2 reads, C1 workflows may create. Storage follows
  `datalake/azure-data-platform-design.md`.
- `household_id` is a scope key; development uses `demo-household`. No Household
  feature, authentication, login, registration, accounts or membership management.

## HITL rules for future implementations

1. Read-only queries, inference, normalization and planning may precede approval.
2. Resolve the proposed change, then pause BEFORE any persistent mutation.
3. Group related writes for one user intent into one approval where appropriate.
4. Approval covers only operations and relevant values actually shown to the user.
5. Changed target, quantity, location, selected purchase option or other relevant
   value requires fresh approval.
6. Retries of the same logical write reuse the same operation identity. UI retries,
   timeouts, lost responses and Agent resume must never duplicate logical writes.
7. Backend validation is mandatory; frontend validation is not authorization.
8. `POST /agent/messages` writes no domain data. Its card tools, `complete_purchase`
   (v8 flow C) and `add_stock` (a new item not from the Shopping List, Issue #168;
   also more of an item the household has, as an `adjustment`, Issue #206, unless
   `NESTOCK_STOCK_INCREASE` is `0`, `false`, `off` or `no`), store a pending
   approval card and return it.
   `POST /agent/resume` writes only what that stored card holds, after the checks in
   `contracts/approval-lifecycle.md`. Cards need Cosmos; without it the Agent offers
   no card tool and `/agent/resume` returns 503. Do not interpret generic proposed
   values or a client boolean as permission to write.

## Owners and intentionally open decisions

| Owner | Extension area | Owner-defined decisions |
| --- | --- | --- |
| A1 | `backend/app/agent`, `tools`, Agent contracts | LangGraph graph/state, tool registry, checkpoints, interrupt/resume, final protocol and approval validation |
| A2 | future adapters in `services`/`tools` | Vision, naming normalization, image processing/storage, commerce provider, purchase-option retrieval |
| B1 | domain models and persistence under `services` | Cosmos containers, partition keys, indexes, embedding/referencing, atomic persistence and durable idempotency |
| B2 | `TransactionHistory` and future forecast services | Forecast/consumption algorithms, reorder thresholds, refresh and history aggregation |
| C1 | shopping service and future orchestration | ShoppingService internals, complete_purchase, cross-domain persistence |
| C2 | `frontend` | Scene visuals, responsive hotspots, page/overlay/sheet presentation, animation, final view-model and wire mapping |

Transaction storage/partitioning, history aggregation and complete_purchase
atomicity require cross-owner decisions. The Cosmos design is in
`datalake/azure-data-platform-design.md`; do not infer it from models.
Leave missing owner-specific decisions as interfaces/documented gaps.
Cross-owner decisions are tracked as GitHub Issues (since 2026-11-04, Issue #29).
`contracts/open-decisions.md` only records the earlier items and points to where
each was decided. Follow the decision in a closed Issue. If the relevant Issue is
still open, do not choose an answer yourself; tell the user.

## Frontend scope

Keep one scene-based Agent Home with conversation/input, photo entry, inventory
cabinet and shopping-cart entry points. Inventory and Shopping remain independent
views. Inject `AgentClient`; never expose LangGraph internals to widgets. Keep
reusable loading/error states, generic result cards and approval extension points.
Do not add primary three-tab navigation or spend skeleton work on final art/animation.

## Excluded scope

No speculative P1: full Box/QR, notification center, scheduler, push notifications,
analytics, transaction-history UI, prediction/promotion dashboards, settings,
advanced animation, external checkout or payment. Do not add common-but-unagreed
fields or entities. Agent approval storage and resume follow
`contracts/approval-lifecycle.md`; add no Agent write path outside it. The App's direct
Shopping List add agreed in Issue #52 is a separate, C1-owned path.

## Validation and collaboration

Read `README.md`, `docs/architecture.md`, and `contracts/README.md` before changing
boundaries. Run Flutter format/analyze/test for frontend changes and backend
ruff/pytest for backend changes. Keep dependency lockfiles. Add meaningful tests
for changed behavior; do not claim unavailable platform checks passed. Never
commit credentials, SDK paths, local environments or build output.
