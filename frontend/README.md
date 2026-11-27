# Nestock｜窩有 frontend

Android/iOS scene-based Flutter application. See the [root README](../README.md)
for setup, validation, and ownership boundaries.

The entry point injects `AgentClient` and `CatalogClient`. By default,
`HttpAgentClient` sends messages to the backend `POST /agent/messages` endpoint.
It preserves structured result data for presentation and renders inventory items
when returned. Photo-aware requests can forward an uploaded, opaque `image_ref`.
Backend Pydantic models remain the contract source of truth; Dart view types and
the current HTTP mapping are provisional.

Cloud builds use `API_BASE_URL` and `MEDIA_API_BASE` over HTTPS, and set
`NESTOCK_GOOGLE_CLIENT_ID` to the Google Web OAuth client ID with
`--dart-define`. The same client ID must be configured as the backend
`NESTOCK_GOOGLE_CLIENT_ID`. The first sign-in may require Google account
selection; later installs silently restore the same account's default
household when Android can restore its Google session. The backend verifies the
Google ID token and returns a separate Nestock bearer. Configure the Android
package name and signing SHA-1 in the Google OAuth client; never put a client
secret in the app.

### STG debug/test APK

From the repository root, build a staging APK with:

```powershell
.\deploy\build-stg-debug-apk.ps1
```

The script reads the STG API URL and Google client ID from Cloud Run, supplies
them as Dart defines, and verifies the client ID is embedded in the resulting
APK. It stops before invoking Flutter when the client ID is missing. It requires
authenticated `gcloud` access; do not copy the client ID into source or local
configuration files.

Release APKs use a separate explicit Gradle `release` signing configuration
with a new private key. The release helper verifies a certificate lineage from
the v1.0.6 Android debug certificate to the new key. Android 9+ accepts the
rotated signer for in-place updates with app data preserved; the lineage
disallows rollback to the old signer. Android 8.1 and earlier continue using
the legacy signer for compatibility. The helper verifies the minimum supported
Android API 24 and the rotated API 28 boundary. It requires the new keystore,
passwords, and alias through `NESTOCK_ANDROID_KEYSTORE_PATH`,
`NESTOCK_ANDROID_STORE_PASSWORD`, `NESTOCK_ANDROID_KEY_ALIAS`, and
`NESTOCK_ANDROID_KEY_PASSWORD`. Keep the private key and its encrypted backup
outside source control.

## Release version

`pubspec.yaml` is the Android release metadata source: `MAJOR.MINOR.PATCH` is
the app version name and the `+BUILD` suffix is the monotonically increasing
Android version code. The current app version is `1.0.7` (build `8`). Preserve
the home-to-Connect navigation and approved scene artwork in release builds.

## C2 development

For Android Emulator, the HTTP clients default to `http://10.0.2.2:8000`.
Override the base with `--dart-define=MEDIA_API_BASE=http://127.0.0.1:8000`
for an iOS simulator or local host, or use a reachable host address on a physical
device. Camera capture and photo upload use injectable clients; successful uploads
retain the returned `image_ref`. The composer also supports push-to-talk recording
and sends audio to `POST /api/speech/transcribe`; the transcript is inserted into
the input for review and manual sending.

For a physical Android device using `adb reverse tcp:8000 tcp:8000`, set
`MEDIA_API_BASE` to `http://127.0.0.1:8000`. Local staging upload URLs remain
on localhost. With the emulator default `http://10.0.2.2:8000`, local staging
upload URLs are adapted to the emulator host address.

For local Agent fixtures, use `--dart-define=AGENT_TRANSPORT=mock` and optionally select
`--dart-define=MOCK_SCENARIO=batchAction`. Scenarios are `conversation`
(default), `completed`, `singleAction`, `batchAction`, `purchaseOptions`,
`partialSuccess`, `error`, and `timeout`. Mock transport opens the home directly
without household onboarding and uses local demo inventory/shopping data; it
does not write to the backend. Photo upload and speech still require an
authenticated live household session and are unavailable in this mode. These
are presentation fixtures, not backend lifecycle or wire statuses.

Vision and Confirmation presentation fixtures are test-only injected `AgentClient`
responses under `test/agent_fixtures.dart`; no user-entered runtime command opens
either fixture. Confirmation remains a no-write demonstration; its fixture is not
a Vision or upload contract.

The HTTP client does not implement `/agent/resume` and rejects pending approvals.
Persistent HITL writes and Cosmos writes are not implemented in the frontend;
Vision confirmation/write flow is not complete. No client-side approval authorizes
backend writes.

Inventory cabinet and toolbar shortcuts open the same room overlay using a
read-only mock presentation client. Search matches names and aliases locally.
Opening Inventory hides an open Work Surface without discarding its local edits;
closing Inventory reveals it again. A new Agent response closes Inventory. This
is presentation coordination, not backend cancellation or resume semantics.
