# STG Connect Page 1.0.2

## Topology and boundaries

- Flutter Connect UI calls the existing authenticated identity API through `IdentityApi` and `IdentityController`; bearer credentials remain in OS secure storage.
- FastAPI serves identity and household routes on the existing STG Cloud Run service. `get_authenticated_install` resolves bearer identity; `get_authorized_household` verifies membership for household-scoped issuance.
- `IdentityService` owns membership and invite rules. `FirestoreIdentityRepository` performs invite consumption and membership writes atomically. Invite plaintext is returned once; only its hash is persisted.
- Existing Pydantic identity request/response contracts and routes remain the integration boundary. No contract or environment-variable changes are required.
- Connect exposes only household selection, household creation, invite-code sharing, and invite-code joining. Existing hand-drawn room/card/Noki assets are reused. The UI copy is Traditional Chinese.

## Invite rejoin correction

A fresh invite must be redeemable by an authenticated install with no active membership in that household. An existing revoked membership is replaceable atomically; an active membership remains ineligible for a second membership. Redemption still consumes a valid invite once and rejects expired or already redeemed invites.

## Release

Flutter and backend release version: `1.0.2`; Android build number: `3`. No new environment variables, ingress, IAM, resource, or service-setting changes are in scope.
