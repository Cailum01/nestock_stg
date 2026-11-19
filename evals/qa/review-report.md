# QA non-test review

**Review date:** 2026-11-18 (Asia/Taipei)<br>
**Review type:** Passive configuration and credential-pattern review; no tests or
runtime probes were run.

## Scope and method

Reviewed these tracked configuration files only:

- `backend/pyproject.toml`
- `deploy/Dockerfile`
- `deploy/azure-blob-lifecycle-policy.json`
- `frontend/android/app/build.gradle.kts`
- `frontend/android/app/src/main/AndroidManifest.xml`
- `frontend/android/app/src/debug/AndroidManifest.xml`
- `frontend/android/app/src/profile/AndroidManifest.xml`
- `frontend/android/build.gradle.kts`
- `frontend/android/gradle.properties`
- `frontend/android/gradle/wrapper/gradle-wrapper.properties`
- `frontend/android/settings.gradle.kts`

A local PowerShell scan checked those files for common credential-shaped
assignments, bearer tokens, and private-key headers. `git ls-files` was used to
check tracked file names for `.env` and private-key files. Secret values were not
printed. Local environment files were not opened.

## Findings

1. **P2 — Release build uses the debug signing key.** The Android release build
   type selects `signingConfigs.getByName("debug")` in
   `frontend/android/app/build.gradle.kts`. This is suitable only for development;
   do not treat a release APK as production-ready until release signing is defined.
   No evidence indicates that a release APK has been distributed.
2. **P2 — Confirm the trusted proxy boundary before cloud exposure.** The backend
   Docker command enables proxy headers and trusts forwarded headers from `*` in
   `deploy/Dockerfile`. The file comment says forwarded headers are used to build
   staging upload URLs. Confirm that deployment ingress overwrites these headers
   and that the application is not directly reachable from untrusted clients.
   This is a configuration risk to verify, not a confirmed exploitable defect.

## Results and limitations

- No credential-shaped values were detected in the reviewed configuration files.
- No tracked `.env` or private-key file names were found.
- Android manifest review found Internet, camera, and microphone permissions;
  no explicit `usesCleartextTraffic="true"` or custom network-security config was
  present in the reviewed manifest. This is static observation, not runtime proof.
- The review did not scan application source, local `.env` files, ignored files,
  generated/build output, or deployment-side settings. It cannot certify the
  complete repository or a deployed environment as secret-free.
- Cloud proxy/ingress configuration and release artifact signing were not
  independently verified.
- Automated, device, E2E, load, and service tests were **not run** in this review.

## Follow-up

- Confirm the proxy trust boundary with the deployment owner before relying on
  forwarded host/protocol values.
- Keep debug APKs limited to development/testing; define release signing before
  any release distribution.
- Use [`coverage.md`](./coverage.md) to track requirement-based acceptance
  evidence separately from this static review.
