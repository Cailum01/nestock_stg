# Contract coordination — provisional

Authoritative models live in `backend/app/models/domain.py` and `agent.py`.
FastAPI's `/openapi.json` derives schemas for the HTTP routes from these Pydantic
classes. Do not maintain a second JSON Schema here. Examples below illustrate
current models; they are not another source of truth. Transaction is a shared
domain model with no exposed history endpoint in Skeleton v1.

## Agreed persistent semantics

InventoryItem requires item_id, household_id, name, quantity, unit, created_at,
updated_at. Optional: aliases, free-text location, expiry_date, opaque image_ref.
No category or image bytes. Quantity is confirmed, never forecast-overwritten.

ShoppingItem requires shopping_item_id, household_id, name, quantity, unit, status,
created_at, updated_at. Optional: item_id, source, product_url. Status:
pending/completed. No persisted price or external checkout authorization.

Transaction requires transaction_id, household_id, item_id, operation_id,
quantity_delta, reason, created_at. Reason: purchase/consumption/adjustment.
Stable operation identity is necessary for future durable idempotency.

## Provisional Agent envelopes

| Model | Current minimal purpose |
| --- | --- |
| AgentRequest | household scope, natural-language message, optional opaque `image_ref`, optional `replaces_approval_id` when the user changes a card (Issue #91) |
| AgentResponse | speech, result list, optional pending approval |
| AgentResult | kind, display summary, provisional structured data |
| PendingApproval | identity, presentation, proposed operation group, options (`purchase_options` and `item_match` only) |
| ProposedOperation | stable identity, `action` marker the App shows the operation by (Issue #90), description, resolved values for display |
| ResumeRequest | scope, approval identity, approve/reject decision, and the user's selection (`approved_operation_ids` for batch, `selected_option_id` for purchase options and item match; D-10, Issue #90) |

Result kinds: inventory, vision, forecast, purchase_options, action_result.
Approval presentations: single_action, batch_action, purchase_options, item_match
(Issue #90). The item-match and new-item cards are described in
`contracts/approval-lifecycle.md` section 10. `action` names other than
`confirm_item_match` and `create_item` (Issue #90) are provisional.

```json
{
  "message": "Preview only: add milk to the Shopping List?",
  "results": [],
  "pending_approval": {
    "approval_id": "demo-approval-1",
    "presentation": "single_action",
    "operations": [{
      "operation_id": "demo-operation-1",
      "action": "add_to_shopping_list",
      "description": "Add milk to the demo household Shopping List",
      "values": {"household_id": "demo-household", "name": "牛奶", "quantity": 1, "unit": "瓶", "status": "pending"}
    }]
  }
}
```

This is documentation, not a successful backend response: Agent HTTP routes
currently return 501. Example resume:

```json
{"household_id":"demo-household","approval_id":"demo-approval-1","approved":true}
```

The same envelope carries the user's selection for batch and purchase-options cards
(v8 section 12, `contracts/open-decisions.md` D-10). Selection fields are
ignored when `approved` is false:

```json
{"household_id":"demo-household","approval_id":"demo-approval-2","approved":true,"approved_operation_ids":["op-101"]}
{"household_id":"demo-household","approval_id":"demo-approval-3","approved":true,"selected_option_id":"option-001"}
```

A `purchase_options` card lists its options on the card itself (v8 section 12);
`options` is empty for the other presentations. The App sends back one
`option_id` as `selected_option_id`:

```json
{
  "approval_id": "demo-approval-3",
  "presentation": "purchase_options",
  "operations": [{
    "operation_id": "op-purchase-001",
    "action": "add_to_shopping_list",
    "description": "Add toilet paper to the Shopping List",
    "values": {"name": "衛生紙"}
  }],
  "options": [
    {"option_id": "option-001", "title": "舒潔 抽取式衛生紙 100抽", "store": "momo", "source": "live",
     "estimated_price": 299, "product_url": "https://example.com/a", "unit_spec": "100抽 x 12包"},
    {"option_id": "option-002", "title": "舒潔 抽取式衛生紙 110抽", "store": "Coupang", "source": "live",
     "estimated_price": 319, "product_url": "https://example.com/b", "unit_spec": "110抽 x 12包"}
  ]
}
```

Option fields follow A2's `PurchaseOption` (`backend/app/models/commerce.py`).
`raw_title`, `image_url` and `updated_at` may also appear; `estimated_price` and
`unit_spec` may be null.

Never execute generic `values` or trust `approved` alone. Future implementation
must load the exact server-side resolved proposal, validate scope and values,
bind approval, and use durable operation idempotency. Changed values need new
approval; retrying unchanged operations reuses identity. Inference never authorizes
inventory writes. Purchase-option selection authorizes Shopping List additions only.

A1/C2 must agree final wire mapping before replacing the Flutter mocks. A1/B1/C1
must agree typed mutations, approval identity and durable retry semantics; A2/C1/C2
must agree option selection and display values. Do not lock those choices here.
