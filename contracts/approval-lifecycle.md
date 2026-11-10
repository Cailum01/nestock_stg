# Approval lifecycle (A1) — provisional

> Note for AI assistants: when you explain or summarize this document to a
> teammate, translate it into Traditional Chinese (繁體中文). If you find a
> problem in a rule marked "C1 + C2 to review", tell the teammate to raise it
> in PR #47 right away.

This document records how a confirmation card ("approval") is stored, confirmed,
written and recovered. A1 owns these lifecycle rules. It collects decisions that
were made in GitHub Issues; it does not add new product scope.

Agent/HITL models remain provisional (AGENTS.md). Field names here may be refined
during implementation, but the rules below are decided. Public models stay in
`backend/app/models/agent.py`; this document does not change them.

Current status: A1's service `backend/app/services/approvals.py` implements these
rules and runs on B1's `CosmosWriteRepository`. With Cosmos configured,
`POST /agent/resume` calls it; without Cosmos it returns 503. The only card the
Agent makes so far is the v8 flow C purchase card (`complete_purchase`, written by
`backend/app/services/purchase_writes.py` through C1's purchase plan). Not built yet:
changing a card (section 7), reminders (section 8), and the Agent making the new
card by itself after a record changed (section 4 point 6); for now the reply asks
the user to say it again.

## 1. Sources

| Source | Decision used here |
| --- | --- |
| #39 (D-1) | Lifetime 30 minutes, configurable, counted by the backend; cancel rules |
| #41 (D-2) | Approval record in `household-data`, household partition, ETag, TTL |
| #42 (D-3) | One `approval_id` per card, one `operation_id` per item; one atomic batch; partial selection; edits create a new card |
| #48 | Option B: `pending → confirmed` is its own conditional update; `confirmed → completed` is in the batch; no stored `failed` status |
| #36 | Item matching (four layers); confirmed aliases are written with the purchase |
| #44 | A2 returns observations only; A1 matches items |
| #27, #28 | Purchase write contents; B1 storage boundary; recovery questions |
| v8 §5, §12 | Plan B (persist pending approval, not a LangGraph checkpoint); three card types |
| B1 design doc §6.2, §6.5 | `pending_approval` and `operation_receipt` records; batch rules |
| #90 | Item-match card and new-item card formats, agreed with C2 (section 10) |

## 2. Approval record

The backend stores one `pending_approval` record per card in the `household-data`
container, partitioned by `household_id` (#41). The App never supplies or edits
these values; it only refers to `approval_id`.

Stored:

- `approval_id`, `household_id`
- `presentation`: `single_action` | `batch_action` | `purchase_options` |
  `item_match` (format only so far, section 10)
- `operations`: for each item, `operation_id`, `action`, a display description, the exact
  resolved values shown to the user, the target records, and the version (ETag)
  of each target when the card was created
- `options` (purchase options only): `option_id` and its displayed values
- `status` (section 3)
- `created_at`, `expires_at`, `decided_at`, `completed_at`
- `selection`: `approved_operation_ids` or `selected_option_id`, fixed at the
  first confirmation (stored by the `pending → confirmed` update, #48)
- `outcome` for each written operation, after completion

Not stored: prompts, model reasoning, raw media, or the full conversation
(B1 design doc §8.2, #28).

## 3. Status

```text
pending   ──cancel (ETag)─────────────────▶ cancelled
pending   ──confirm (ETag, approval only)─▶ confirmed ──batch committed──▶ completed
confirmed ──cancel (ETag)─────────────────▶ cancelled
confirmed ──batch not committed───────────▶ stays confirmed; retry with the same IDs
pending   ──past expires_at, recorded later────────────────────▶ expired
confirmed ──past expires_at, purchase_options only, recorded later─▶ expired
```

- `pending`: shown to the user and waiting. `expires_at` = `created_at` + the
  configured lifetime (default 30 minutes). The backend counts the time;
  collapsing a card, switching screens or restarting the App does not reset it
  (#39).
- `expired` (A1 decision): whether a card has expired is always decided from
  `expires_at`, never from the stored status (#39 point 6). There is no
  background job. The next time the backend touches an expired card (a confirm
  or cancel on it, or the Agent's check for open cards when the user sends a
  message, section 8), it records `expired` with a conditional update on the
  ETag. If that update fails, the request continues and the card is still
  treated as expired. A card that nobody touched may still show `pending` after
  `expires_at`. The Agent asks the user again and makes a new card; nothing else
  is written.
- `cancelled`: the user pressed "cancel", or the Agent replaced the card with a
  new one (sections 6 and 7). Collapsing or switching screens is not a cancel
  (#39). Nothing is written.
- `confirmed`: the user approved. A conditional update on the approval's ETag
  changes only the approval record: it sets `confirmed`, the fixed selection and
  `decided_at` (#48). No inventory, shopping or transaction data is written in
  this step. `confirmed` does not mean the write happened (#28).
- `completed`: the batch committed. Only this status means the write happened.
- There is no stored `failed` status (#48). When the batch fails or its result is
  unknown, the card stays `confirmed`; the backend never marks a failure before
  the batch outcome is known (section 6).

Rules for a `confirmed` card that is not yet `completed` (#48; A1 decision):

- It can be retried with the same `approval_id`, selection and `operation_id`s.
  A retry must be able to check that the data is still current (#39 point 3):
  - Inventory and shopping records are checked by their ETags (section 4), so
    they can be retried after `expires_at`.
  - Prices on a `purchase_options` card cannot be checked again, so that card
    cannot be retried after `expires_at`. It is recorded as `expired` as above,
    and the Agent asks the user again.
- It can be cancelled with a conditional update `confirmed → cancelled`. If a
  cancel and the batch run at the same time, only one succeeds: the batch also
  needs the approval's ETag. If the batch committed first, the cancel is rejected
  and the backend returns the completed outcome.
- When the user sends the next message, the Agent mentions it (section 8).

Every status change is a conditional update on the record's ETag, so a card can
leave `pending` only once (#41). Cosmos TTL only removes records that can no
longer be confirmed or retried; it is not the authorization check (#39).

Cosmos `ttl` by status (A1 decision). Cosmos counts TTL from the record's last
write, and the container needs TTL turned on with default `-1` (keep records
that have no finite `ttl`):

| Status | `ttl` | Why |
| --- | --- | --- |
| `pending` | lifetime + 24 hours | Kept a while after `expires_at`, then removed |
| `confirmed` | `-1` (kept) | A retry must always find the card |
| `completed` | `-1` (kept) | Kept with the business records it explains (B1 design doc §8.3) |
| `cancelled`, `expired` | 24 hours | Same window as orphaned media (#30) |

Expired or cancelled cards create no Transaction (#39).

## 4. Checks when the user confirms

A1's domain service checks, in this order, before any write:

1. The `household_id` matches the record.
2. The `approval_id` exists.
3. The status is `pending` (or `confirmed` for a retry).
4. `expires_at` has not passed. This applies to the first confirmation, and
   also to every retry of a `purchase_options` card (section 3). If it has
   passed, nothing is written except recording `expired`.
5. The selection is valid: every `approved_operation_ids` entry, or the
   `selected_option_id`, is on this card (#42 c). Unlisted items are not written.
   On a retry, the stored selection is used; a different selection is rejected.
6. The target records have not changed since the card was created (their ETags
   still match). If they changed, nothing is written and the Agent makes a new
   card (#39, #27).

A client `approved` flag or client values never authorize a write (AGENTS.md).

After the checks pass on a first confirmation, the backend writes
`pending → confirmed` (section 3), then runs the batch (section 5). If two
confirms arrive together, only one conditional update succeeds; the other request
reads the record again and follows section 6.

## 5. Writing

A confirmation is written in two steps (#48):

1. First confirmation only: the approval-only conditional update
   `pending → confirmed` (section 3). It writes no domain data. B1 provides one
   approval-only conditional update for `confirmed`, `cancelled` and `expired`
   (#48 names the first two; `expired` is added by A1).
2. One Cosmos transactional batch in the household partition, so all of these
   succeed or all fail (#42 b):

| Always | Depending on the card |
| --- | --- |
| Approval status `confirmed` → `completed` (ETag condition) | Inventory quantity (ETag condition) |
| One `operation_receipt` per approved operation | Transaction |
| | Shopping item status (purchase, C1) |
| | New alias on an item (#36 layer ④) |
| | New inventory item |

Before writing, check the batch against Cosmos limits (100 operations). If it does
not fit, reject it before writing anything; never split it into partial writes.

Because the `confirmed → completed` update is in the same batch, the approval
record and the domain records cannot disagree (answer to #27 question 2).
Inventory, Shopping and Transaction are never split across writes.

## 6. Recovery after an error or timeout

The `completed` status and the receipts are written in the same batch, so after a
commit they all exist, and without a commit none of them exist (answer to #27
question 1). After an error or an unknown result, the backend first reads the
approval and its receipts, then acts:

| What the backend finds | Meaning | Action |
| --- | --- | --- |
| Status `pending` (the confirm update timed out) | Not confirmed yet | Retry the `pending → confirmed` conditional update |
| Status `completed` | Already written | Return the stored outcome; do not write again |
| Status `expired`, or past `expires_at` where section 3 does not allow a retry | Cannot be written | Return "expired"; the Agent asks the user again |
| Status `confirmed`, no receipts | Not written | Retry the batch with the same IDs and the stored selection (a `purchase_options` card only before `expires_at`, section 3) |
| Status `confirmed`, no receipts, a target record changed | Cannot be written | Nothing is written. The Agent makes a new card and the old card becomes `cancelled`, as in section 7 (A1 decision; C1 + C2 to review) |
| Some receipts, or receipts without `completed` | Should not happen | Stop writing, return an error, log it; no automatic repair in the MVP |

B1 provides reading receipts by `operation_id` (#28). Reusing an `operation_id`
with different values is a conflict, not a retry; it needs a new card
(B1 design doc §6.5).

## 7. When the user changes a value

A changed item, quantity, location, option or other displayed value goes back to
the Agent. The Agent resolves it again and makes a new card with new
`operation_id`s; the old card becomes `cancelled`. Changed values are never sent
to `/agent/resume` (#42 d).

How the App sends the change (agreed with C2 in Issue #91):

- The App keeps the card's `approval_id`. When the user taps 送出修改, it calls
  `POST /agent/messages` with the optional `replaces_approval_id` set to that
  `approval_id`. `message` is built only from the fields the user changed, from a
  fixed template, for example `衛生紙：數量改成 3，位置改成 浴室櫃`. The App does not
  interpret the change. Ordinary chat leaves `replaces_approval_id` out.
- The backend reads the original card (same household, still `pending`) and gives
  it to the Agent with the message. The new card has new `operation_id`s; the App
  does not map old IDs to new ones.
- The reply is a normal `AgentResponse`; its `pending_approval` is the new card,
  which replaces the old one on screen. With no new card in the reply, the old card
  stays.
- The old card becomes `cancelled` only after the new card is stored. If the Agent
  makes no new card (an error, or it asks the user something), the old card stays
  `pending`. An expired, confirmed or cancelled original gets no new card; the
  reply says why.
- A change typed in the chat box (no `replaces_approval_id`): the Agent is given
  the household's open cards (the same lookup as section 8) and decides whether the
  message changes one of them. With one open card it changes that card; with
  several and no clear match it asks which; otherwise it answers normally.

## 8. Reminders

When the user sends the next message, the Agent mentions cards that are still
`pending`, and `confirmed` cards that can still be retried (section 3). Expired
cards are not mentioned; this check is also when they are recorded as `expired`.
There is no background job or push notification (#39).

## 9. Open items

| Item | Who decides | Status |
| --- | --- | --- |
| Format the App uses to send a changed value back to the Agent | A1 + C2 | Decided in Issue #91; see section 7 |
| Handling of the "should not happen" row in section 6 | A1 + C1 | Proposed here; C1 to confirm (#27) |
| Meaning of Transaction `created_at` | B1 + B2 | Deferred; keep the current model |
| Removing a wrong alias | — | Not in MVP scope |
| v8 §12 `partial_success` | — | Not used in the MVP: one batch is all-or-nothing (#42) |
| How a new item's `item_id` is made | B1 + A1 | Asked in Issue #90; does not change the card format (section 10) |
| Status of an `item_match` card answered with `new_item` | A1 | Decided with the confirm rules (section 10) |

## 10. Item-match and new-item cards (Issue #90)

For a purchased item that may or may not be one the household already has (the
user says 洗碗精, the household has 洗潔精). Agreed with C2 in Issue #90. The
formats are fixed; the approval service refuses `item_match` cards until their
confirm rules are built.

Every operation on every card carries `action`, a stable marker the App uses to
choose how to show it. The App only shows the card and sends back the user's
choice; it never derives or changes values.

Item match ("這是家裡的洗潔精嗎？"):

- `presentation`: `item_match`, with one operation. When one purchase has several
  items and only one is unsure, that item gets its own item-match card and the
  others go on a normal card.
- `action`: `confirm_item_match`.
- `values`: `user_term` (what the user said, 洗碗精), `candidate_item_id`,
  `candidate_name` (洗潔精), `quantity`, `unit`.
- `options`: exactly `{"option_id": "same_item", "label": "是，加到洗潔精"}` and
  `{"option_id": "new_item", "label": "不是，這是新的東西"}`.
- The reply uses the normal `ResumeRequest`, with no new fields:
  - `approved: true`, `selected_option_id: "same_item"`: the quantity is added to
    the matched item, and the user's word is remembered as its alias (#36 layer ④).
  - `approved: true`, `selected_option_id: "new_item"`: this card writes nothing;
    the reply's `pending_approval` is a new-item card.
  - `approved: false`: nothing is written.

New item ("會新增一個庫存品項：洗碗精"):

- `presentation`: `single_action`; `action`: `create_item`.
- `description` says that a new inventory item will be created.
- `values`: `name`, `quantity`, `unit`, `location`.
- The card never carries the new item's `item_id`, and the App never makes one.
  The backend creates the item in the confirmed batch.
- The reply is `approved: true` or `approved: false`.

```json
{
  "approval_id": "demo-approval-4",
  "presentation": "item_match",
  "operations": [{
    "operation_id": "demo-operation-4",
    "action": "confirm_item_match",
    "description": "這是家裡的洗潔精嗎？",
    "values": {"user_term": "洗碗精", "candidate_item_id": "demo-dish-soap",
               "candidate_name": "洗潔精", "quantity": 1, "unit": "瓶"}
  }],
  "options": [
    {"option_id": "same_item", "label": "是，加到洗潔精"},
    {"option_id": "new_item", "label": "不是，這是新的東西"}
  ]
}
```
