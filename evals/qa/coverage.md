# QA acceptance coverage

This is a requirement-derived coverage map. Expected behavior comes from
`demo-readiness-checklist-V3.md` and the cited closed issue decisions. Each row
records its own evidence status; evidence must identify the App/backend revision
and dataset.

## Final Demo coverage

| ID | Requirement source | Journey / observable acceptance | Evidence still needed |
|---|---|---|---|
| QA-01 | PRD §1, §6 Android | Physical Android app reaches the cloud HTTPS API without localhost, emulator networking, or ADB reverse. | **Pass, read-only smoke 2026-11-18:** phone request reached the main HTTPS API. Exact deployed backend revision remains unverified. |
| QA-02 | PRD §3.B | Inventory questions return the intended item, confirmed quantity, and location without an unrelated primary result. | **Pass, read-only 2026-11-18:** Agent returned 8 packs of tissue at the bathroom cabinet. Prompt was English; no unrelated primary result appeared. |
| QA-03 | PRD §3.C | Forecast queries show the intended low-stock and normal-stock cases, use the expected history, and do not alter confirmed quantity. | Dataset, reference time, prompts, response and before/after quantity evidence. |
| QA-04 | PRD §3.D; §2 C1; Issues #179/#180 | Shopping opens and its contents agree with Agent shopping reads. | **Pass, read-only 2026-11-18:** Agent and Shopping UI both listed 麵粉、牙膏; neither exposed planned quantities. |
| QA-05 | PRD §3.E; §2 A1/A2/C2 | Photo upload, vision, matching, and the applicable approval card complete; business records remain unchanged before confirmation. | Physical-device flow, approval identity, before/after business-record read-back. |
| QA-06 | PRD §3.F | Purchase card shows the correct target and user-confirmed actual quantity; confirmation updates Inventory and Shopping consistently and writes exactly one purchase Transaction. | Isolated baseline, card values, approval/operation IDs, post-write read-back. |
| QA-07 | PRD §3.G | Retrying the same completed approval, including a lost-response retry, creates no duplicate effective write. | Same approval/operation IDs and before/after read-back. |
| QA-08 | PRD §3.H | Cancelling a separate approval changes no Inventory, Shopping, or Transaction business state. | Isolated baseline, cancellation outcome and before/after read-back. |
| QA-09 | PRD §5; §6 Demo data | The authorized reset procedure restores the agreed dataset and supports repeatable rehearsal. | Current full-partition preflight, explicit data-owner authorization, reset/read-back evidence. |
| QA-10 | PRD §2 A2/C2 | Physical Android camera and microphone flows work; permission, upload, and transcription failures are surfaced honestly. | Device, permission state, service/source mode, success and recoverable-failure evidence. |
| QA-11 | PRD §6 Cloud / Backend | Cloud health and required service connectivity are verified without presenter-laptop credentials. | Deployment revision, HTTPS health result, intended runtime identity and service smoke evidence. |

These are coverage targets, not claims that the flows are implemented or passed.
The readiness checklist's evidence record remains authoritative for execution
status.

## Coverage changes from recent decisions

**Reviewed:** 2026-11-18<br>
**Relevant merged PRs:** #181, #186, #187, #189, #190, #192, #195, #196,
#199, #200, #201, #203.<br>
**Requirements are traced to the readiness checklist and closed issue decisions;
PRs identify the delivered scope, not the expected behavior.**

| ID | Requirement source | Updated acceptance scenario | Current evidence / guard |
|---|---|---|---|
| QA-12 | PRD §3.D; Issues #179/#180 | Shopping UI and explicit Shopping-list queries present item names without exposing legacy planned quantity; missing/null quantity and unit stay absent, never defaulted. | **Pass for visible item names/no planned quantity 2026-11-18** on the phone and main HTTPS target. Null/missing-field variants not separately exercised. |
| QA-13 | PRD §3.F; Issues #178/#182/#180 | For a linked pending item with planned quantity 1, user-confirmed actual purchase quantity 2 is accepted. Inventory and the purchase Transaction use 2; planned quantity is neither the default nor an upper bound and Shopping does not present it. | Write test only with an isolated, authorized household and before/after read-back. |
| QA-14 | PRD §3.E; Issue #175; approval lifecycle §10 | “Same item” confirms one purchase against the resolved existing item, completes its Shopping row, writes one purchase Transaction, and does not remember an alias. “Not the same” writes nothing. A photo does not create an item-match card. | Write branches need isolated authorized data. Mixed certain/uncertain items follow the issue decision: do not create multiple cards in one message. |
| QA-15 | PRD §2 A1/B1/C1; Issue #168; approval lifecycle §10 | `add_stock` for an item not on Shopping creates a pending card and no pre-confirm domain changes. Cancel/reject writes nothing; confirm creates the Inventory item, adjustment Transaction, and ChangeEvent, with no Shopping row; retry has one effective result. | Historical cloud evidence exists, but that run added five items to `demo-household`. Do not repeat there or reset it; use an isolated authorized household. |
| QA-16 | PRD §2 C2; Issues #91/#188; approval lifecycle §§7–7.1 | Photo/new-item cards can edit name, quantity, and location together. The server returns a replacement card; confirmation writes only its stored values. Invalid edits/cancel do not write. Purchase lines do not inherit new-item name/location edits. | Issue #188 reports Android/Cloud E2E; verify the deployed revision before treating it as current evidence. |
| QA-17 | PRD §3.E/F; Issues #176/#202; PRs #199–#203 | On successful photo-backed approval, the response is not held for the background post-commit handoff; the attached media leaves staging and is retained. Cancel, expiry, or an uncommitted write does not hand off media. Duplicate confirmation must not duplicate the handoff or fail the completed business write. | #176 remains open. The missing composite index was corrected and its query was read-tested, but a post-#203 purchase-with-photo handoff has no recorded real-Cosmos E2E result. Do not claim retention is fully accepted. |
| QA-18 | PRD §3.F; Issue #162 | Dated purchase behavior is off by default. Only when both backend `NESTOCK_DATED_PURCHASES=1` and App `NESTOCK_DATED_PURCHASES=true` are intentionally enabled in an authorized test environment should past dates appear/edit; future/unreadable dates are not accepted. | Do not toggle either flag on the shared main deployment for this test. |
| QA-19 | PRD §3.C; Issues #157/#195 | Forecast remains read-only: `confirmed_quantity` matches Inventory, and insufficient history returns `insufficient_data`. | Issue #157 records a prior cloud read-only check with 20 items and no quantity mismatches (4 estimated, 16 insufficient-data); historical evidence, not a fresh run against the current deployment. |
| QA-20 | PRD §3.E; PR #189 | When a photo approval card is shown, do not also show a redundant standalone Vision result for the same photo. | Presentation check; recheck on the current physical-device build. |

### Existing cloud evidence and safe test boundary

- Issue #157 records cloud F/G/H purchase, same-card retry, and cancel on the
  isolated household `a1-cloud-20261116114237`. It does **not** cover Android,
  lost-response retry, or current deployment revision. Its `/forecast` follow-up
  was read-only. Preserve this as historical evidence; do not repeat F/G/H against
  `demo-household`.
- Issue #168 records `add_stock` and photo-card work but also states that five
  items were added to `demo-household`; the agreed Demo data is not a disposable
  test fixture. No reset or further writes to that household without current data
  owner authorization and a reviewed recovery plan.
- Issue #176 is still open. In particular, purchase-with-photo retention after
  PR #203 and safe retry against real Cosmos remain unverified in its latest
  evidence. The #202 index/query check alone does not close that E2E gap.
- The main HTTPS `/health` returned 200 on 2026-11-18. This confirms reachability
  only; the deployed commit/revision has not been verified from the health response.
- The current phone build points to the main HTTPS API and its frontend tree has
  no diff from local `origin/main`; still record the actual deployed backend
  revision before attributing an E2E result to “main”.

## Exploratory candidates for PM confirmation

No explicit acceptance threshold is defined for these. Keep them exploratory until
the PM confirms whether they belong in the Final Demo:

| Scenario | Requirement source |
|---|---|
| TalkBack focus order, text scaling, and contrast on Agent, Inventory, Shopping, and approval surfaces. | exploratory (no explicit requirement) |
| App background/resume and network interruption during read-only requests and approval submission. | exploratory (no explicit requirement) |
| Two devices viewing or acting on the same household item/card at nearly the same time. | exploratory (no explicit requirement) |
| Empty and unusually large Inventory/Shopping lists; long Traditional Chinese item names and locations. | exploratory (no explicit requirement) |
| Approval expiry near the expiry boundary and device clock differing from server time. | exploratory (no explicit requirement) |

## Evidence discipline

- Record exact App/backend revision, test environment, household/dataset identity,
  relevant approval/operation IDs, command or user steps, result, and verdict.
- Mark unavailable devices/services and skipped scenarios as blocked or not run,
  never as passed.
- Do not reset shared or live data without a current read-only preflight and
  explicit data-owner authorization.
- Do not infer expected behavior from implementation details when requirements are
  silent or conflicting; route the question to the PM.
