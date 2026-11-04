# Nestock Azure Database User Guide

**Audience:** Nestock developers and their AI coding assistants
**Environment:** Shared development resources in the school Azure subscription
**Status:** Development infrastructure is provisioned. The backend has an
opt-in, read-only Cosmos integration; production data model and write integration
are not implemented.

## 1. What is ready

The following development resources have been created and verified:

| Resource | Value |
| --- | --- |
| Subscription | `Azure subscription 2 (Metaage)` |
| Tenant ID | `2e44da17-d0e9-42f9-98f6-4221f20368c3` |
| Resource group | `GAE26512RG` |
| Cosmos DB account | `nestock-cosmos-dev-20261101` |
| Cosmos DB API | Azure Cosmos DB for NoSQL |
| Cosmos DB location / capacity | Southeast Asia / Serverless |
| Database | `nestock` |
| Container | `household-data` |
| Container partition key | `/household_id` |
| Cosmos composite index | `/created_at` descending, then `/transaction_id` ascending |
| Cosmos endpoint | `https://nestock-cosmos-dev-20261101.documents.azure.com:443/` |
| Storage account | `stnestockdev20261101` |
| Storage SKU / location | Standard LRS / Southeast Asia |
| Hierarchical namespace | Enabled (ADLS Gen2) |
| Blob container | `media` (private; anonymous access disabled) |
| Blob endpoint | `https://stnestockdev20261101.blob.core.windows.net/` |
| ADLS Gen2 DFS endpoint | `https://stnestockdev20261101.dfs.core.windows.net/` |

The account, database, container, and private media container exist. This does
**not** mean the production schema, complete indexing policy for every approved
query, seed data, or write integration is complete. Setup did not insert domain
or sample records; check the environment before assuming it remains empty. The
dev container has the composite index required
by the current Transaction ordering query, defined in
[cosmos-indexing-policy.json](./cosmos-indexing-policy.json); a future approved
feature-keyword query may require another index after its query plan is verified.

These resources are in a shared resource group because the school role cannot
create a separate group. Do not change or delete unrelated resources in
`GAE26512RG`.

## 2. Important project boundary

The Flutter app remains offline and uses mocks. The backend has an optional,
read-only Cosmos adapter for Inventory and Shopping. It is disabled by default;
without the explicit environment configuration below, the backend continues to
serve demo data. The adapter does not enable writes, approval persistence,
transaction-history endpoints, or Blob access. Its internal repository supports
household-partition-scoped lists and stable-ID point reads for Inventory and
Shopping; no new public route is added for point reads.

Do not treat this read adapter as authorization. The current API uses the fixed
`demo-household` scope; the Cosmos partition key is not a security boundary.
Obtain affected-owner agreement before adding any write path or changing domain
contracts.

### Optional read-only Cosmos connection

Use the signed-in Entra identity through `DefaultAzureCredential`; do not use
account keys or connection strings. The current account is serverless, so
queries consume billable request units even though the container has no
provisioned throughput.

In PowerShell, after `az login` and selecting the Nestock development
subscription, configure all three values to enable the adapter:

```powershell
$env:NESTOCK_COSMOS_ENDPOINT = "https://nestock-cosmos-dev-20261101.documents.azure.com:443/"
$env:NESTOCK_COSMOS_DATABASE = "nestock"
$env:NESTOCK_COSMOS_CONTAINER = "household-data"
```

Restart the backend after setting these variables. Clear them to return to the
offline demo services. A partial configuration is an error; the application
does not silently fall back to demo data. The identity needs a Cosmos SQL
data-plane role scoped to the `household-data` container. The adapter executes
parameterized queries with the `/household_id` partition key and performs no
Cosmos mutations.

Inventory list queries omit records with a non-null, persistence-only
`deleted_at` marker, as specified by the platform design. This read behavior does
not add a delete/restore operation or change the `InventoryItem` API model.

- Cosmos DB records are shared development data; use synthetic data only.
- Confirm the active Azure subscription before running the backend.
- Do not create another Cosmos account, database, container, or Storage account;
  the shared development resources already exist.
- Blob/ADLS integration and lake tables are not configured by this adapter.
  Keep the existing private `media` container. Any Data Lake table format,
  catalog, or analytics workload must follow the pending D-7 decision.
- Pydantic models remain the domain/API contract source of truth. This guide and
  the data-platform design are not a replacement schema.
- Cosmos records are shared development data. The `/household_id` partition key
  is a query and transaction boundary, **not** per-household authorization.

See [the platform design](./azure-data-platform-design.md),
[the architecture decisions](../docs/architecture.md), and
[the contract guide](../contracts/README.md) before proposing a schema or writes.

## 3. Who has access

The six listed developers have been granted:

- Cosmos DB Built-in Data Contributor, scoped to
  `nestock` / `household-data`.
- Storage Blob Data Contributor, scoped to the `media` container.

This permits reading and changing data within those scopes. It does not grant
access to the entire subscription or resource group. GitHub repository access
is separate from Azure access.

Azure guest invitations must be accepted from the invited school mailbox before
the guest can use the tenant. A developer who cannot sign in or receives
`Forbidden` should check invitation acceptance and the selected tenant /
subscription; do not work around the error with account keys.

At the time these roles were assigned, guest acceptance was still pending for
P01, P03, P04, P05, and P06. P02 is already a tenant user. The owner should
confirm pending invitations have been accepted before expecting those five
developers to connect.

The Cosmos account currently has public network access enabled and no IP
firewall rules. Entra ID authentication and narrow data roles are configured,
but the public endpoint is reachable from the internet. Use synthetic,
non-personal development data only. Do not put real household, personal, or
production data in these resources.

## 4. Sign in from a developer machine

Install Azure CLI and sign in with the invited school account:

```powershell
az login --tenant 2e44da17-d0e9-42f9-98f6-4221f20368c3
az account set --subscription 186e9d87-a1a6-4dcd-8033-005369690f26
az account show --query "{name:name, tenant:tenantId, user:user.name}" --output table
```

Confirm the shown user is the intended school identity and the tenant matches.
Never paste passwords, device codes, access tokens, account keys, SAS tokens, or
connection strings into chat, issues, source files, or prompts.

If using a different organization account, first accept the guest invitation
and then select the Nestock tenant in the sign-in flow. The subscription will
only be usable if that identity has been added and granted access.

## 5. Authentication for local scripts

Use Microsoft Entra ID credentials from Azure CLI; do not use account keys.
For Python experiments, create a local virtual environment outside committed
application dependencies, then install the SDKs there:

```powershell
python -m venv "$env:USERPROFILE\.venvs\nestock-azure"
& "$env:USERPROFILE\.venvs\nestock-azure\Scripts\Activate.ps1"
python -m pip install azure-identity azure-cosmos azure-storage-blob
$env:NESTOCK_COSMOS_ENDPOINT = "https://nestock-cosmos-dev-20261101.documents.azure.com:443/"
$env:NESTOCK_STORAGE_ACCOUNT_URL = "https://stnestockdev20261101.blob.core.windows.net/"
```

Do not commit the virtual environment. The endpoint URLs are identifiers, not
credentials. `DefaultAzureCredential` can use the Azure CLI session after
`az login`.

The following read-only smoke check prints a count for the synthetic
`demo-household` partition and checks that the private media container is
reachable. It does not list or print document contents or blob names:

```python
import os

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

credential = DefaultAzureCredential()

cosmos = CosmosClient(
    os.environ["NESTOCK_COSMOS_ENDPOINT"],
    credential=credential,
)
container = (
    cosmos.get_database_client("nestock")
    .get_container_client("household-data")
)

counts = list(
    container.query_items(
        query=(
            "SELECT VALUE COUNT(1) FROM c "
            "WHERE c.household_id = @household_id"
        ),
        parameters=[{"name": "@household_id", "value": "demo-household"}],
        partition_key="demo-household",
    )
)
print(f"demo-household document count: {counts[0] if counts else 0}")

blob_service = BlobServiceClient(
    account_url=os.environ["NESTOCK_STORAGE_ACCOUNT_URL"],
    credential=credential,
)
media = blob_service.get_container_client("media")
media.get_container_properties()
print("private media container: reachable")
```

Save experiments as local, untracked files. Do not add credentials or temporary
scripts to the repository. If authentication fails, report the error and ask a
resource owner to check the assignment; never switch to a shared key.

## 6. Data access rules for developers and AI assistants

1. **Start read-only.** Inspect the agreed model and existing records before
   proposing a mutation. Do not dump whole documents, personal text, or blob
   names into chat or logs.
2. **Always scope Cosmos queries.** Filter by `household_id` and pass the same
   value as the SDK `partition_key`. Do not issue cross-partition queries unless
   the owner explicitly approves a concrete need.
3. **Treat partition scope as shared access.** Current RBAC allows each
   developer with the Cosmos role to access the whole `household-data` container;
   it does not isolate a person's household. Use synthetic test data only.
4. **Do not invent fields or record types.** Follow the current Pydantic models
   and owner-approved decisions. Proposed records in the design remain
   provisional where marked.
5. **Do not write directly from a feature adapter or AI proposal.** Business
   writes belong in domain services after backend validation and applicable
   human approval. A prompt, UI confirmation, `approved: true`, or inferred
   quantity is not authorization.
6. **Preserve inventory invariants.** Predicted quantity never overwrites
   confirmed `InventoryItem.quantity`. Quantity changes require user-provided
   information and applicable approval.
7. **Preserve transaction history.** Do not edit or delete append-only
   transactions to repair attribution. Use the approved transaction-link
   behavior only after relevant owners have agreed.
8. **Use Blob for opaque media objects only.** Keep the container private; do
   not put raw media, Base64, personal filenames, or secrets in Cosmos fields,
   source control, or logs. Never create a public container or share a SAS/key.
9. **Be careful with destructive operations.** Do not delete containers,
   databases, accounts, or records, or change firewall, networking, retention,
   indexing, or capacity settings without explicit owner approval.
10. **Surface errors.** Do not silently fall back to another tenant, account,
    key-based authentication, unscoped queries, or a different household.

### Suggested instruction for an AI coding assistant

> This is the shared Nestock development Azure environment. Read
> `README.md`, `AGENTS.md`, `docs/architecture.md`,
> `contracts/README.md`, and `datalake/azure-data-platform-user-guide.md`.
> Follow owner boundaries. Flutter remains offline; backend Cosmos reads are
> opt-in and read-only. Use Entra ID, never account keys. Begin with scoped,
> read-only inspection; use only synthetic data. Cosmos queries must filter and
> set the `/household_id` partition. Do not mutate, delete, create resources,
> change infrastructure, or add write integration without explicit owner
> approval and the required human-approval/domain-service flow. Do not print or
> transmit document contents, blob names, or credentials.

## 7. Data model and query status

The current Cosmos container is a provisioned storage boundary, not a fully
implemented application schema. The platform design proposes a
`record_type`-discriminated `household-data` container partitioned by
`/household_id`; the design also lists record concepts such as `inventory_item`,
`transaction`, `operation_receipt`, and `change_event`. These concepts are not
all implemented as Cosmos document schemas.

Before creating or changing record shapes, confirm the applicable owner
decisions. In particular:

- Transaction changes proposed by B2 (optional `item_id` and
  `feature_keywords`) still require unanimous contract approval.
- B2 owns forecast and transaction-history aggregation; B1 stores source
  records and does not pre-aggregate.
- Atomic write, idempotency, append-only linking, indexing, retention, and
  deletion behavior must follow the design and owner approvals.
- Do not treat Cosmos DB's `/household_id` key as authentication or
  authorization.

The design's query contract and boundaries are described in Sections 6.4–6.6
of [the platform design](./azure-data-platform-design.md). This guide does not
add a second query or document schema.

## 8. Cost and operational safety

Cosmos Serverless and LRS reduce some costs but are not free. The subscription's
credit is shared with other resources in `GAE26512RG`; this setup does not impose
a hard spending cap or automatically stop services when credits are exhausted.
Check Azure Cost Management regularly and coordinate before changing capacity,
regions, replication, or retention.

When the opt-in backend Cosmos adapter is enabled, each Inventory or Shopping
read logs Cosmos request charge, result count, and elapsed time. List reads
aggregate request charge across pages; point reads use the single response
charge. Not-found point reads log `result_count=0` and use the charge from the
exception response header when available, otherwise `request_charge=unavailable`.
These diagnostics omit household IDs and document contents. Use request charge
as per-read telemetry; it does not include storage, backup, or other subscription
costs and is not a spending cap.

Do not use the resources for production workloads. Keep the existing private
Blob access and avoid widening Azure role scopes. If a team member leaves,
notify the resource owner so both their GitHub access and Azure guest/data-role
assignments can be reviewed and removed.

## 9. Troubleshooting

| Symptom | Check |
| --- | --- |
| `az login` succeeds but data requests return `Forbidden` | Confirm the invited school account is active in the Nestock tenant, the correct subscription is selected, and the guest invitation was accepted. |
| Cosmos SDK reports unauthorized | Ensure the developer used `DefaultAzureCredential` after `az login`; verify Cosmos data role assignment and wait briefly for RBAC propagation. Do not use account keys as a workaround. |
| Blob SDK reports authorization failure | Confirm the operation targets the `media` container and the account uses the signed-in Entra identity. The Blob role does not grant account-key access. |
| Portal cannot open the Cosmos account | Data-plane permission is not the same as Azure resource-management Reader permission. Ask the owner for the minimum additional portal visibility only if needed. |
| A query returns no items | The setup did not seed domain data. Verify the selected household and partition key; do not broaden to an unscoped query just to find data. |
| A command appears to target another subscription | Stop and verify `az account show` before retrying. Do not continue with a different default account. |
