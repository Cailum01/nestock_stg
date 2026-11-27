# Cosmos deployment helpers

## Build the STG Android APK

From the repository root, run:

```powershell
.\deploy\build-stg-apk.ps1
```

The release build uses an explicit Android `release` signing configuration.
Before building, the helper requires these four environment variables to be
available locally: `NESTOCK_ANDROID_KEYSTORE_PATH`,
`NESTOCK_ANDROID_STORE_PASSWORD`, `NESTOCK_ANDROID_KEY_ALIAS`, and
`NESTOCK_ANDROID_KEY_PASSWORD`. Supply them through a protected local
environment or approved secret store; never put credential values in source,
`.env`, shell history, or build logs. The key file must be owned by the current
Windows user and grant access only to that user, `SYSTEM`, and local
Administrators.

The helper verifies that the configured certificate has SHA-256 fingerprint
`7e940a27cde13001c0cffd0b676c3723ea8cc2622f5ecb35a729bad6af396d27` before
building, then verifies the APK signature against the same fingerprint.
Gradle's release build no longer falls back to the debug signing configuration.
The existing keystore is used as-is; the build does not generate or modify key
material. A missing, inaccessible, or mismatched key blocks both dry-run and
build.

### Release-ready workflow

1. Land the intended source and version change on `origin/stg`; check out its
   current `stg` commit with no staged, unstaged, deleted, or untracked files.
   The build script does not fetch, switch branches, clean, commit, push, or
   bump versions.
2. Set `frontend/pubspec.yaml` to `major.minor.patch+build`. Both the app
   version and Android build number must be greater than those in the latest
   published stable `stg-vMAJOR.MINOR.PATCH` GitHub release. The script reads
   that release's tagged `pubspec.yaml` using read-only `gh` queries and never
   changes the candidate version.
3. Optionally run `.\deploy\build-stg-apk.ps1 -DryRun`. It performs the same
   source, release, gcloud, live API, and Flutter preflight checks without
   building. A blocked gate is reported with its reasons and returns a
   non-zero exit code; the real build is refused until every gate passes.
4. Run the single build command above. It reads the API URL and
   `NESTOCK_GOOGLE_CLIENT_ID` from the `nestock-stg-api` Cloud Run service in
   `nestocke-stg` / `us-central1`, verifies live `/openapi.json` exposes
   `/identity/google`, then supplies the API, media, OAuth, and HTTP transport
   Dart defines. The client ID is not printed or duplicated in a local `.env`.

The build is blocked if the checkout is not clean `stg`, `HEAD` differs from
either the local tracking ref or live `origin/stg`, the GitHub release/version
cannot be read, the candidate app version or build number is not newer, the
active gcloud project/service/client ID is invalid, live OpenAPI lacks the
identity route, Flutter is unavailable, or the configured signer is missing,
inaccessible, or does not match the approved certificate. Fix the source,
version, or local signer configuration and land source changes on `origin/stg`;
the script never bypasses these checks.

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
other than `-1`. It treats Azure CLI's empty JSON output for an unset default
TTL as `$null`, so Windows PowerShell 5.1 does not need special configuration.
