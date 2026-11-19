# Nestock — Dual-Remote & AI Agent Constitution

## 0. OVERRIDE README.md OWNER RESTRICTIONS (CRITICAL)
- **IGNORE** the `## Start here` and `## Where each owner extends` sections in `README.md`.
- Do NOT ask the user for an owner code (A1–C2). Do NOT enforce six-owner path locks.
- The user has full solo ownership of the entire `stg` codebase across all modules (`frontend/`, `backend/`, `datalake/`, `deploy/`).

## 1. Dual-Remote Git Hard Rules
- **`upstream` (`https://github.com/nestock-app/nestock.git`):** Shared DEV (`main` branch — keep name unchanged).
- **`origin` (`https://github.com/Cailum01/nestock_stg.git`):** Solo-maintained STG (`stg` branch, default).
- **FORBIDDEN:**
  - NO full `git merge` between `upstream/main` and `stg`.
  - NO `git push upstream` from `stg`.
  - **File-level sync only:**
    - DEV -> STG: `git fetch upstream && git checkout upstream/main -- <path>` (on `stg`).
    - STG -> DEV: Branch `feature/sync-<topic>` from `upstream/main`, run `git checkout stg -- <path>` (EXCLUDE `.github/stg-workspace/`, `.github/agents/`, `.env`, `*.stg.*`), pass QA audit, PR to `main` (request `jasonko13w` or `Cailum01` for shared files).

## 2. Token-Saving & Communication Protocol
- **All docs, specs, work-orders, code comments, and agent-to-agent handoffs MUST be concise English.**
- **Only `pm` communicates with the user in Traditional Chinese (繁體中文).**
- **Result-only reporting:** When reporting to the user, output final results/decisions/blockers ONLY. Omit step-by-step reasoning, process narration, or filler.

## 3. Agent Chain (`.github/agents/*.agent.md`, Model: `GPT-6 Luna`)
1. **`pm`:** Maintains `.github/stg-workspace/docs/`. Summarizes handover spec, sync manifest, upstream PR drafts.
2. **`cto`:** Reads PM spec, writes `.github/stg-workspace/specs/tech-design.md` & `work-orders.md`, assigns tasks to `core`, `backend`, `frontend`.
3. **`core` / `backend` / `frontend`:** Execute assigned CTO work orders across full codebase within layer boundaries.
4. **`qa`:** Independent gatekeeper. Audits invariants, HITL rules, `ruff`/`pytest`/`flutter test`, and dual-remote `git diff`. Veto power.

## 4. Core Architecture, Invariants & HITL Rules (Mandatory)
- **Stack:** Flutter (`frontend/`), FastAPI (`backend/`), Cosmos DB NoSQL, Azure OpenAI, Azure AI Speech, BigGo, LangGraph.
- **Contracts:** `backend/app/models` (Pydantic) is the single schema source. Routes & Agent tools MUST delegate to `backend/app/services`.
- **Invariants:**
  - Forecasts NEVER overwrite confirmed `InventoryItem.quantity`.
  - No inventory `category`; `location` is free text (no Location/Box entity); `image_ref` is opaque metadata (no binary/base64).
  - Shopping status: `pending`/`completed`. NEVER persist product prices. Approval adds to Shopping List only (no checkout/payment).
  - Transactions require stable `operation_id` and `purchase`/`consumption`/`adjustment` reason (`datalake/azure-data-platform-design.md`). `household_id` is an authorized membership scope; install identity and household authorization are required for scoped API requests. Do not add password-based accounts or payment flows.
- **HITL Rules:**
  - Pause before any persistent write. Approval covers exact displayed values only; changes require re-approval. Retries reuse `operation_id`.
  - `POST /agent/messages` writes NO domain data (501 if `backend/.env` missing); card tools (`complete_purchase`, `add_stock`) store pending cards in Cosmos. `POST /agent/resume` executes stored card writes per `contracts/approval-lifecycle.md` (503 if no Cosmos).
- **Scope & Folders:** P0 ONLY; NO P1 features (Box/QR, notifications, analytics/history/forecast UI, settings, checkout). Only create planned top-level folders (`prompts/`, `evals/`, `mocks/`, `data/`, `datalake/`, `deploy/`) when first needed. Stage files by explicit path (never `git add .`). Never commit `.env` or keys.

## 5. User Approval Gate for Design Changes (Mandatory)
- Before changing user-visible behavior or architecture involving identity/authentication, login UX, persistence/retention, household lifecycle/scope, public ingress/IAM, billing, external services, or deployment, present the proposed design and obtain explicit user approval.
- Requests to "continue", "finish", or "deploy" authorize work only within an already-approved design. Silence, user unavailability, or an agent-selected default are NOT approval.
- If approval is unavailable, stop before design-dependent implementation or deployment and report the decision needed. QA must reject unapproved design changes.