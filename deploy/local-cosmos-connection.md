# Connect the backend to the shared Cosmos dev account (local)

Provisional (B2). Resource details: `datalake/azure-database-user-guide.md`.

## 1. Sign in

```bash
az login --tenant 2e44da17-d0e9-42f9-98f6-4221f20368c3
az account set --subscription 186e9d87-a1a6-4dcd-8033-005369690f26
```

Authentication is `DefaultAzureCredential` (your Entra identity). Do not use account
keys. Your user needs the Cosmos DB Built-in Data Contributor role on `household-data`.

## 2. Configure `backend/.env` (gitignored; never commit)

```
NESTOCK_COSMOS_ENDPOINT=https://nestock-cosmos-dev-20261101.documents.azure.com:443/
NESTOCK_COSMOS_DATABASE=nestock
NESTOCK_COSMOS_CONTAINER=household-data
```

All three are required; a partial configuration is an error. With none set, the backend
serves offline demo data.

The backend loads `backend/.env` when `app.core.dependencies` is imported. Real
environment variables take precedence over the file. Tests set `NESTOCK_SKIP_DOTENV=1`
(`backend/tests/conftest.py`) so they never reach a real Cosmos account.

## 3. Start and verify

```bash
cd backend
uv run uvicorn app.main:app --port 8000
curl localhost:8000/inventory
```

The "Cosmos is not configured, so forecasts use demo transaction history" warning must not
appear. An empty `[]` is expected while `demo-household` has no records; offline demo mode
would return sample items instead.

## Notes

- The container is shared development data; use synthetic data only.
- Cloud hosting (managed identity, env vars on the host) is covered in
  `b1-cosmos-api-integration.md`.
