# Cosmos deployment helpers

## Enable per-record TTL on `household-data`

The approved container policy is `defaultTtl = -1`: documents without a
per-record `ttl` remain stored, while documents with a finite `ttl` become
eligible for Cosmos cleanup. TTL is cleanup only; it is not an approval
expiration or authorization check.

The script targets only the specified SQL container and changes only its
default TTL. It does not change the partition key, indexing policy, or records.
By default it reads and reports the current setting without making changes.

```powershell
.\deploy\set-cosmos-household-ttl.ps1 `
  -Subscription <subscription> `
  -ResourceGroup <resource-group> `
  -AccountName <cosmos-account> `
  -DatabaseName <database>
```

Before applying, inspect whether existing documents already contain finite
per-record TTL values: setting the container default enables those TTLs to take
effect. After that review, apply and verify the setting:

```powershell
.\deploy\set-cosmos-household-ttl.ps1 `
  -Subscription <subscription> `
  -ResourceGroup <resource-group> `
  -AccountName <cosmos-account> `
  -DatabaseName <database> `
  -Apply `
  -ConfirmExistingTtlReviewed
```

The script requires Azure CLI authentication and permission to update and read
the target container. It stops rather than replacing a non-null default TTL
other than `-1`.
