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
`POST /agent/resume` calls it; without Cosmos it returns 503. The Agent makes three
kinds of card. One is the v8 flow C purchase card (`complete_purchase`, section 11;
read back by A1's `backend/app/services/purchase_inputs.py` and written through C1's
`ShoppingWriteBuilder`), including new-item lines for entries not yet in inventory
(section 10). Another is the item-match card for an entry that may be a household
item under another name (section 10, Issue #175; also from `complete_purchase`,
written as a purchase). The third is the new-item card from a photo (section 10,
Issue #124; written through B1's `PhotoItemWriteBuilder`). Changing a card with 送出修改 is built (section 7, and sent as data in
section 7.1), and an open card comes back with the next message (section 8). A
purchase day the user says is built but off (section 11). Not built yet: the Agent making the
new card by itself after a record changed (section 4 point 7); for now the reply
asks the user to say it again.

## 1. Sources

| Source | Decision used here |
| --- | --- |
| #39 (D-1) | Lifetime 30 minutes, configurable, counted by the backend; cancel rules |
| #41 (D-2) | Approval record in `household-data`, household partition, ETag, TTL |
| #42 (D-3) | One `approval_id` per card, one `operation_id` per item; one atomic batch; partial selection; edits create a new card |
| #48 | Option B: `pending → confirmed` is its own conditional update; `confirmed → completed` is in the batch; no stored `failed` status |
| #36 | Item matching (four layers); layer ④, remembering an alias, is not built (Issue #175) |
| #44 | A2 returns observations only; A1 matches items |
| #27, #28 | Purchase write contents; B1 storage boundary; recovery questions |
| v8 §5, §12 | Plan B (persist pending approval, not a LangGraph checkpoint); three card types |
| B1 design doc §6.2, §6.5 | `pending_approval` and `operation_receipt` records; batch rules |
| #90 | Item-match card and new-item card formats, agreed with C2 (section 10) |
| #175 | Item-match card: 「是」 writes the purchase in one card and remembers no alias; 「不是」 writes nothing; one unsure item per card; no photo item-match card (section 10) |

## 2. Approval record

The backend stores one `pending_approval` record per card in the `household-data`
container, partitioned by `household_id` (#41). The App never supplies or edits
these values; it only refers to `approval_id`.

Stored:

- `approval_id`, `household_id`
- `presentation`: `single_action` | `batch_action` | `purchase_options` |
  `item_match` (section 10)
- `operations`: for each item, `operation_id`, `action`, a display description, the exact
  resolved values shown to the user, the target records, and the version (ETag)
  of each target when the card was created
- `options` (purchase options and item match only): `option_id` and its displayed
  values
- `status` (section 3)
- `created_at`, `expires_at`, `decided_at`, `completed_at`
- `selection`: `approved_operation_ids` or `selected_option_id`, fixed at the
  first confirmation (stored by the `pending → confirmed` update, #48)
- `outcome` for each written operation, after completion
- `replaces_approval_id`: only on a card made by 送出修改, the card it replaces
  (section 7)

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
  new one (sections 6 and 7), or the card was a replacement whose original could
  not be cancelled (section 7, reason `replace_failed`). Collapsing or switching
  screens is not a cancel (#39). Nothing is written.
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
- It is not brought back with the next message (section 8): not built.

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
5. A card made by 送出修改 counts: its original is `cancelled` with reason
   `replaced` (section 7). If not, nothing is written and the card becomes
   `cancelled` (reason `replace_failed`).
6. The selection is valid: every `approved_operation_ids` entry, or the
   `selected_option_id`, is on this card (#42 c). Unlisted items are not written.
   On a retry, the stored selection is used; a different selection is rejected.
7. The target records have not changed since the card was created (their ETags
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
- If cancelling the old card raises an error, the old card is read back first. If
  it reads back `cancelled` with reason `replaced`, the cancel was written but its
  reply was lost, and the new card stands. Any other status counts as not replaced.
- If the old card does not end up `cancelled` with reason `replaced` after the new
  card is stored, the reply is not a success: the new card is cancelled too (reason
  `replace_failed`) and the reply carries no card. It tells the user to confirm or
  cancel the original card, or cancel it and say the change again; if the original
  was meanwhile confirmed, cancelled by the user or recorded as `expired`, it says
  that instead, in the same words as for such an original before the change.
- The new card records the card it replaces (`replaces_approval_id`, section 2) and
  counts only once that card is `cancelled` with reason `replaced`. A card that does
  not count is never mentioned (section 8) and cannot be confirmed (section 4). So
  even if cancelling the old card and withdrawing the new card both fail, the user
  never holds two cards that can be confirmed; the new card is logged and expires.
- A change typed in the chat box (no `replaces_approval_id`) is not a change. The
  reply brings the open card back (section 8), and the Agent asks the user to change
  it on the card and tap 送出修改 (Issue #91, 2026-11-16; this replaces the earlier
  plan where the Agent judged which open card the message changed).

Built (A1): 送出修改 with `replaces_approval_id`. The Agent is given the old card's
lines (without `transaction_id`) and makes the new card with `complete_purchase`; a
missing, expired, confirmed or cancelled original gets a reply with the reason and no
card, and the model is not called; an original that does not end up replaced withdraws
the new card, and a new card counts only once its original is replaced, as described
above. A typed change is answered as in section 8.

### 7.1 送出修改 sent as data (`card_changes`, Issue #162)

Agreed with C2 on 2026-11-16 (Issue #162) so that a changed day or quantity does not
depend on the model reading a sentence. Built (A1); the App switches to it later,
and until then the sentence above keeps working.

- The App calls `POST /agent/messages` with `replaces_approval_id` and
  `card_changes`: one entry per changed line, by the `operation_id` on the card,
  with only the changed fields. `message` is still required and is only the chat
  text. `quantity` is a whole number ≥ 1. `purchased_on` is a Taipei date
  `YYYY-MM-DD`; an explicit `null` sets the line back to today (no day on the card).
  A field left out, and every line not listed, keeps its value.

```json
{
  "household_id": "demo-household",
  "message": "送出修改",
  "replaces_approval_id": "a-old",
  "card_changes": [
    {"operation_id": "op-1", "quantity": 3},
    {"operation_id": "op-2", "purchased_on": "2026-11-14"},
    {"operation_id": "op-3", "purchased_on": null}
  ]
}
```

- A malformed body is refused with 422 before anything runs: `card_changes` without
  `replaces_approval_id`, an empty list, an entry without `quantity` or
  `purchased_on`, `quantity` null, not a whole number or below 1, or a
  `purchased_on` that is not a date.
- Otherwise the reply is 200. The backend reads the stored original card (it is the
  authority, not the App), puts every line on a new card with the changes applied,
  read from the current records, and retires the original as in section 7. The
  model is not called. On success `pending_approval` is the new card and
  `results[0].data` is `{"code": "card_changed", "replaced_approval_id": ...}`.
- A refused change stores nothing. `results[0].data.code` says why, and while the
  original is still `pending` the reply's `pending_approval` is that original
  again (same `approval_id`), so it stays on screen:

| `code` | When |
| --- | --- |
| `card_not_changeable` | The original is missing, expired, confirmed, completed or cancelled (`reason`), or was not replaced; no card in the reply |
| `unknown_operation` | An `operation_id` is not on the original (`operation_id`) |
| `duplicate_operation` | The same `operation_id` is listed twice |
| `nothing_changed` | Every value is the same as on the original |
| `purchase_day_off` | A day is changed, or the original has one, while dated purchases are off (section 11) |
| `future_date` | `purchased_on` is after today in Taipei |
| `line_refused` | A line cannot go on a new card: it cannot be recorded with the current records, a photo line is given a day, or the line cannot be changed on the card. `lines` has one entry per refused line: its `operation_id` on the original, `shopping_item_id` for a line from the Shopping List, and `reason`. A new card never leaves a line out |

Photo new-item cards (section 10, Issue #124) are changed the same way, only while
photo cards are on:

- Only `quantity` changes. A photo line has no purchase day, so a `purchased_on`
  (even `null`) refuses it (`line_refused`), whether or not dated purchases are on.
- Every line goes on the new card with its `name`, `unit` and `location` (null) as
  on the original, a changed line with its new `quantity`, a line not listed with its
  old one. The card keeps its presentation (`single_action` or `batch_action`).
  Every line gets a new `operation_id` and `transaction_id`, and the card a new
  `approval_id`; the original is retired as in section 7.
- Before the new card is stored, each line is checked against the household's items
  again: a line whose name is now the name or alias of a household item is refused
  (`line_refused`), so confirming never adds a second item.
- With photo cards off (no Cosmos, section 10), a photo card cannot be
  changed on the card: `line_refused`, each line with reason 這一項不能在卡上修改。
- A photo card is changed only with `card_changes`. The sentence format in section 7
  is for purchase cards: the Agent's card tool makes purchase cards only.

```json
{
  "message": "沒有出新卡：「滑鼠」照片新增的品項沒有購買日期，不能改日期。",
  "results": [{"kind": "action_result", "data": {"code": "line_refused",
    "lines": [{"operation_id": "op-1", "reason": "照片新增的品項沒有購買日期，不能改日期。"}]}}],
  "pending_approval": "（原卡）"
}
```

## 8. Reminders

The App shows a card only with the reply that brought it. After 清除對話 or reopening
the App, the card is gone from the screen but still open in the backend. So the
reminder is the card itself, not a sentence the user cannot act on (Issue #91,
2026-11-16; C2 confirmed the App side the same day):

- On every `POST /agent/messages` without `replaces_approval_id`, the backend lists
  the household's open cards (`ApprovalService.list_open`). Expired cards are left
  out and recorded as `expired` here. A 送出修改 card that does not count (section 7)
  is left out too.
- If `pending` cards are left, the reply's `pending_approval` is the newest one again:
  the same `approval_id`, lines and expiry. The App shows it like any card; confirm,
  cancel and 送出修改 work as before. The Agent answers the message and says the card
  is not confirmed yet. With several, it says how many are left; each later message
  brings back the newest one still open.
- That message makes no new card. When the user says something else was bought, the
  Agent asks them to confirm or cancel this card first (`complete_purchase` returns
  `open_card_not_answered`). A typed change gets the same answer (section 7).
- `confirmed` cards that can still be retried (section 3) are not shown again: not
  built.
- If the open cards cannot be listed, the message is answered without them, but it
  makes no new card (`complete_purchase` returns `open_cards_unknown`): unknown is
  not "none", and a new card could be a second card for the same purchase (PR #160
  C2 review). The user says it again later.
- A 送出修改 message (with `replaces_approval_id`) skips this lookup; section 7 applies.

Built (A1): `PurchaseCards.open_cards` and `ReadAgent.respond`. There is no background
job or push notification (#39).

## 9. Open items

| Item | Who decides | Status |
| --- | --- | --- |
| Format the App uses to send a changed value back to the Agent | A1 + C2 | Decided in Issue #91; see section 7 |
| Handling of the "should not happen" row in section 6 | A1 + C1 | Proposed here; C1 to confirm (#27) |
| Meaning of Transaction `created_at` | B1 + B2 | Decided in Issue #113: when the event happened; when the user gave no date, the card's first `decided_at`, and a retry keeps that same time (section 11). C1's builder does this |
| Removing a wrong alias | — | Not in MVP scope |
| v8 §12 `partial_success` | — | Not used in the MVP: one batch is all-or-nothing (#42) |
| How a new item's `item_id` is made | B1 + A1 | Decided in Issue #90: B1's `resolve_inventory_item_id(operation_id)` (PR #97), made when the user confirms; see section 10 |
| Status of an `item_match` card answered with `new_item` | A1 | Decided in Issue #175: `cancelled` with reason `not_same_item`, nothing written (section 10) |

## 10. Item-match and new-item cards (Issue #90, #175)

For a purchased item that may or may not be one the household already has (the
user says 洗碗精, the household has 洗潔精). Formats agreed with C2 in Issue #90;
the item-match confirm rules decided in Issue #175 (built in
`backend/app/agent/cards.py` and the approval service).

Every operation on every card carries `action`, a stable marker the App uses to
choose how to show it. The App only shows the card and sends back the user's
choice; it never derives or changes values.

Item match ("這是家裡的洗潔精嗎？"):

- When: the user says a Shopping List entry was bought, the entry is not linked to
  an inventory item, and it may be a household item: the model names the item it
  thinks it is (`complete_purchase` item field `candidate_item_id`, #36 layer ③,
  after looking at the inventory), or exactly one household item has the entry's
  name or alias (layers ①②). Several items with that name or alias make no line.
- `presentation`: `item_match`, with one operation. A card holds one unsure item
  (one card per message, and no new card while one is open, section 8): when other
  items come in the same message, they go on a normal card and the unsure item is
  left off, with a reason telling the user to say it again after that card. With
  only unsure items, the first gets the card and the others are left off the same
  way.
- `action`: `confirm_item_match`.
- `values` shown, and approved by 「是」: `user_term` (the list entry's name, 洗碗精),
  `candidate_name` (洗潔精), `quantity`, `unit` (the entry's unit, or the item's
  when the entry has none; a different unit makes no line).
- `values` fixed but not shown: `candidate_item_id`, `shopping_item_id`,
  `transaction_id`, and `purchased_at` on a dated purchase (section 11). Targets: the
  versions of the shopping entry and of the candidate item, as on a purchase line.
  C1's preview checks the purchase, with the candidate as its resolved target,
  before the card is made.
- `options`: exactly `{"option_id": "same_item", "label": "是，加到洗潔精"}` and
  `{"option_id": "new_item", "label": "不是，這是新的東西"}`, in that order.
- The reply uses the normal `ResumeRequest`, with no new fields:
  - `approved: true`, `selected_option_id: "same_item"`: written like a purchase
    line (section 11): A1's `CardWriteRouter` hands the card to C1's
    `ShoppingWriteBuilder` as a one-line purchase card, and `CardInputResolver`
    reads `candidate_item_id` as the target (`resolved_inventory_item_id`). The
    quantity is added to that item, the entry becomes `completed` and keeps its
    planned quantity, and one purchase Transaction uses the card's `transaction_id`,
    all in one batch; a retry reuses the `operation_id`. No alias is remembered
    (Issue #175), so the same word is asked about again next time. The reply is
    已記錄：<candidate_name> +N <unit>（現在 N <unit>）.
  - `approved: true`, `selected_option_id: "new_item"`: nothing is written; the card
    becomes `cancelled` with reason `not_same_item`. The reply carries no card: it
    asks the user to say the item again with how many and where it goes, which then
    makes a new-item card. When `user_term` is the household item's own name, the
    reply says the household already has it and no second item of that name is
    added.
  - `approved: false`: nothing is written.
  - A different answer than the first is refused (409) or gets the first one's reply.
- The quantity cannot be changed on this card: `card_changes` refuses it
  (`line_refused`); the user cancels and says it again.
- A photo makes no item-match card (Issue #175 point 3).

New item ("會新增一個庫存品項：洗碗精"):

- `presentation`: `single_action`; `action`: `create_item`.
- `description` says that a new inventory item will be created.
- `values`: `name`, `quantity`, `unit`, `location`.
- The card never carries the new item's `item_id`, and the App never makes one.
  The backend creates the item in the confirmed batch.
- The reply is `approved: true` or `approved: false`.

The Agent makes a new-item line today when the user says a Shopping List entry was
bought and the entry is not linked to an inventory item (`complete_purchase`,
`backend/app/agent/cards.py`):

- The line also carries `shopping_item_id` and `transaction_id`, and the version of
  the shopping entry only (the new item does not exist yet). The App does not show
  them. On a `batch_action` card it sits next to ordinary purchase lines.
- When the entry may be a household item (the item-match rules above), the card is
  an item-match card instead. The Agent is told to look at the inventory for the
  same thing under another name (layer ③) before it asks for the card.
- `unit` is the entry's unit, or the unit the user said when the entry has none.
  `location` is what the user said; without it no line is made, and the Agent asks
  where the item goes.
- On confirmation, A1's `CardInputResolver` gives C1 a `NewInventoryTarget` whose
  `item_id` is `resolve_inventory_item_id(operation_id)`. C1's builder creates the
  item, completes the entry, and writes the Transaction and change event in the same
  batch, so a retry never makes a second item.

New-item cards from a photo (Issue #124; on with Cosmos since PR #172):

- After A2's recognition and A1's matching (#44), photo items with no household item
  of the same name or alias (#36 layers ①②), seen with confidence (not
  `low_confidence`) and with a confirmed unit get one card: `single_action` for one
  item, `batch_action` for several, every line `create_item`
  (`backend/app/agent/cards.py`, `PhotoCards`).
- `values`: `name`, `quantity` and `unit` from the photo, `location` null (the App
  shows 未填; confirming accepts no location, Issue #124), and `transaction_id`. There
  is no `shopping_item_id` and no target version.
- Items the household has, unsure items and items without a confirmed unit are only
  reported, as before. A photo item-match card (Issue #124 point 2) is not built.
- Confirming (Issue #124, #168): A1's
  `PhotoInputResolver` (`backend/app/services/purchase_inputs.py`) turns each line in
  the fixed `approved_operation_ids`, in card order, into C1's `AdjustmentInput`
  from the stored card only: `approval_id`, `operation_id`, `transaction_id`,
  `quantity`, and a new item with `name`, `unit`, `location` (null) and `item_id`
  from B1's `resolve_inventory_item_id(operation_id)`. B1's `PhotoItemWriteBuilder`
  (`backend/app/services/photo_item_writes.py`, PR #171) plans each line with C1's
  `plan_adjustment`, using the card's `decided_at` as the time,
  and writes the item, the `adjustment` Transaction, the change event
  (`shopping_item_id` null) and the receipts with the completed card in one batch
  (#42). A1's `CardWriteRouter` (`backend/app/services/card_writes.py`) sends photo
  cards to that builder and purchase cards to C1's `ShoppingWriteBuilder`, in
  `get_approval_service`. Without a photo builder it refuses a photo card, so
  nothing is stored or written. The reply after confirming is 已記錄：<name> +N
  <unit>（現在 N <unit>） per line, like a purchase.
- As in section 8, a photo makes no card in a message that brings an open card back,
  or when the open cards cannot be listed; the photo is only reported.
- The quantity can be changed on the card with `card_changes` (section 7.1); a new
  card replaces the old one, and the user confirms again.
- On in `get_read_agent` whenever cards are (Cosmos): confirming is written by
  B1's builder above. Without Cosmos a photo is only reported.

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

## 11. Purchase card (v8 flow C)

The user says items on the Shopping List were bought (我買了衛生紙 2 包). The
Agent's `complete_purchase` tool makes this card (`backend/app/agent/cards.py`) and
stores it as `pending`; nothing else is written until the user confirms. The format
below is what the code makes; its `values` were agreed with C1 in Issue #60. The App
shows it as a purchase card (C2, PR #101): internal IDs are not shown, and only the
quantity can be changed.

- `presentation`: `single_action` for one item, `batch_action` for several.
- Every operation: `action` `complete_purchase`; `description` such as
  `衛生紙 +2 包（從待購清單完成購買）`.
- `values`: `name`, `purchased_quantity` (a whole number, at least 1),
  `purchased_unit` (the inventory item's unit), `shopping_item_id`, `item_id`,
  `transaction_id`. There is no `location`: the purchase adds to an inventory item
  the household already has.
- An entry linked to an inventory item gets a purchase line only if it is still
  pending and its unit (when it has one) is the inventory unit. The listed quantity
  does not limit the bought quantity (Issue #178). A pending entry not
  linked to an inventory item gets a new-item line instead (section 10, PR #140).
  Other items are left off the card and the Agent says why.
- A Shopping List entry may have no quantity or unit (Issue #52). Without a unit,
  the card uses the inventory unit and the user confirms it on the card. This is
  A1's choice, not a v8 or team rule (v8 does not cover units); A1 supplies
  `purchased_unit` as agreed with C1 in Issue #60.
- The reply uses the normal `ResumeRequest`: `approved: true` or `false` for
  `single_action`; for `batch_action`, `approved_operation_ids` lists the items the
  user kept checked (all of them when nothing was unchecked). Unchecked items are not
  written (Issue #42 c).
- A changed value goes back to the Agent as in section 7 (Issue #91).
- Purchase time (Issue #113): the Transaction's `created_at` is when the purchase
  happened. Without a day on the line, the first `decided_at` is used, and a retry
  keeps that same time.
- A day the user says (昨天買的, A1-50) is built but off in the running app
  (`PurchaseCards(dated_purchases=...)`). With it on, the model is told today's
  Taipei date and may give each item `purchased_on` (YYYY-MM-DD). A past day goes on
  the line as `purchased_at` (12:00 Taipei that day, `purchase_time.py`, PR #71) and
  in its description (「11/15 買的」); today or no day leaves it off; a future or
  unreadable day leaves the item off the card. `purchase_inputs.py` passes
  `purchased_at` on to C1. `NESTOCK_DATED_PURCHASES=1` in the backend's environment
  (`backend/.env` on your own computer) turns it on for testing; any other value, or
  none, is off, and the cloud does not set it. It stays off in the cloud until the
  App shows the day on the purchase card (HITL rule 4; PR #163), the App sends a
  changed day as `card_changes` (section 7.1), and the live-model test of spoken days
  passes (`test_purchase_dates.py`, `-m live_eval`, Issue #127). A changed day needs
  no model: `card_changes` is applied by the backend.

```json
{
  "approval_id": "demo-approval-5",
  "presentation": "single_action",
  "operations": [{
    "operation_id": "demo-operation-5",
    "action": "complete_purchase",
    "description": "衛生紙 +2 包（從待購清單完成購買）",
    "values": {"name": "衛生紙", "purchased_quantity": 2, "purchased_unit": "包",
               "shopping_item_id": "demo-shopping-1", "item_id": "demo-toilet-paper",
               "transaction_id": "demo-transaction-1"}
  }],
  "options": []
}
```
