#!/usr/bin/env bash
# Deploy the Nestock backend to Azure Container Apps (single replica).
# Usage: deploy/deploy-aca.sh   (after `az login`; add SECRETS_FILE=... only for a first deployment)
#   Subscription, resource group and registry are found automatically from the existing
#   Container App; SUB / RG / ACR can still be set to override.
#   SECRETS_FILE holds KEY=VALUE lines (never committed), e.g. the values from
#   backend/.env plus NESTOCK_COSMOS_*. No Cosmos key or client secret is needed: the app's
#   system-assigned identity signs in (DefaultAzureCredential). A NEW app's identity needs the
#   Cosmos data role from B1 (see deploy/b1-cosmos-api-integration.md); the existing
#   `nestock-api` already has it.
# Image pull uses that same identity with AcrPull only. If the account may not create role
# assignments for a ServicePrincipal (the course "student" role: ABAC allows Users only), the
# script falls back to a repository-scoped, pull-only ACR token that expires in 30 days.
# The ACR admin user is never enabled. Rotation and revocation: deploy/demo-day-runbook.md.
# Supported shells: Linux, macOS, or WSL on Windows. Git Bash on Windows is NOT supported
# (observed: role assignment fails with MissingSubscription before the token fallback runs).
set -euo pipefail

# Git Bash stays unsupported, but its worst trap is neutralized here: MSYS rewrites
# arguments that start with "/" (a --scope such as /subscriptions/<id>/...) into C:/Program Files/Git/..., and az then fails with
# MissingSubscription instead of the AuthorizationFailed the token fallback relies on.
# Both variables are ignored on Linux/macOS.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"

# Some az extensions (e.g. containerapp) print "WARNING: ..." lines on STDOUT, which would end
# up inside $(az ... -o tsv) values (a resource group named "WARNING: ...\nGAE..."). Drop those
# lines for every az call; the exit status stays az's own.
az() {
  command az "$@" | { grep -v '^WARNING' || true; }
  return "${PIPESTATUS[0]}"
}

LOC="${LOC:-eastasia}"
ENV_NAME="${ENV_NAME:-nestock-env}"
APP="${APP:-nestock-api}"
SECRETS_FILE="${SECRETS_FILE:-}"

# --- Auto-discovery: no manual subscription / resource group / registry input -------------
# Find the existing Container App across every subscription the signed-in account can see.
if [[ -z "${SUB:-}" || -z "${RG:-}" ]]; then
  FOUND=""
  ERR_FILE="$(mktemp)"
  while read -r sub_id; do
    [[ -z "$sub_id" ]] && continue
    # A failed query (expired login, no permission) is NOT "app not found": stop and show it.
    if ! rg_found="$(az containerapp list --subscription "$sub_id" \
      --query "[?name=='$APP'].resourceGroup | [0]" -o tsv 2>"$ERR_FILE")"; then
      # stderr is kept apart from the value: az extensions print WARNING lines there, and
      # they must never become part of the resource group name.
      echo "Could not list Container Apps in subscription $sub_id (run az login?):" >&2
      cat "$ERR_FILE" >&2; exit 1
    fi
    if [[ -n "$rg_found" ]]; then FOUND="$sub_id|$rg_found"; break; fi
  done < <(az account list --query "[].id" -o tsv)
  rm -f "$ERR_FILE"
  if [[ -n "$FOUND" ]]; then
    SUB="${SUB:-${FOUND%%|*}}"; RG="${RG:-${FOUND##*|}}"
  else
    # First deployment: fall back to the current subscription and its only resource group.
    SUB="${SUB:-$(az account show --query id -o tsv)}"
    if [[ -z "${RG:-}" ]]; then
      GROUPS_FOUND="$(az group list --subscription "$SUB" --query "[].name" -o tsv 2>/dev/null || true)"
      [[ -n "$GROUPS_FOUND" && "$(wc -l <<<"$GROUPS_FOUND")" -eq 1 ]] || {
        echo "Container App $APP not found and there is not exactly one resource group; set RG" >&2; exit 1; }
      RG="$GROUPS_FOUND"
    fi
  fi
fi
echo "Using subscription $SUB, resource group $RG"

# Reuse the existing registry in the group; create one only if none exists.
if [[ -z "${ACR:-}" ]]; then
  ACR="$(az acr list -g "$RG" --subscription "$SUB" \
    --query "[?starts_with(name,'nestockacr')].name | [0]" -o tsv 2>/dev/null || true)"
  ACR="${ACR:-nestockacr$RANDOM}"
fi
echo "Using registry $ACR"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Image tag = build time + the git commit it was built from (+ "-dirty" when backend/, prompts/
# or deploy/ have uncommitted changes), so "which version is running?" is answered by the
# revision's image tag alone. TAG can still be set to override.
if [[ -z "${TAG:-}" ]]; then
  COMMIT="$(git -C "$ROOT" rev-parse --short=7 HEAD 2>/dev/null || echo nogit)"
  if [[ "$COMMIT" != nogit && -n "$(git -C "$ROOT" status --porcelain -- backend prompts deploy 2>/dev/null)" ]]; then
    COMMIT="$COMMIT-dirty"
  fi
  TAG="$(date +%Y%m%d%H%M%S)-$COMMIT"
fi
echo "Image tag $TAG"
CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT
rsync -a --exclude '.env' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude 'tests' "$ROOT/backend/" "$CTX/"
cp "$ROOT/deploy/Dockerfile" "$CTX/Dockerfile"
cp -R "$ROOT/prompts" "$CTX/prompts"

az account set --subscription "$SUB"
az acr show -n "$ACR" -g "$RG" >/dev/null 2>&1 || \
  az acr create -n "$ACR" -g "$RG" -l "$LOC" --sku Basic --admin-enabled false >/dev/null
# Registry-wide admin credentials can push images; make sure they stay off.
az acr update -n "$ACR" -g "$RG" --admin-enabled false >/dev/null
az acr build -r "$ACR" -t "nestock-api:$TAG" "$CTX"

az containerapp env show -n "$ENV_NAME" -g "$RG" >/dev/null 2>&1 || \
  az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOC" >/dev/null

# Secrets. Redeploying an existing app without SECRETS_FILE keeps its current secrets and
# environment variables untouched; a first deployment needs SECRETS_FILE.
APP_EXISTS=false
az containerapp show -n "$APP" -g "$RG" >/dev/null 2>&1 && APP_EXISTS=true
if [[ -z "$SECRETS_FILE" && "$APP_EXISTS" == false ]]; then
  echo "First deployment: set SECRETS_FILE (KEY=VALUE lines, never committed)" >&2; exit 1
fi
# Secrets: KEY=VALUE -> container app secret (lowercase, '-') + env var secretref.
SECRET_ARGS=(); ENV_ARGS=()
while IFS='=' read -r k v; do
  [[ -z "${k// }" || "$k" == \#* || -z "$v" ]] && continue
  s="$(echo "$k" | tr 'A-Z_' 'a-z-')"
  SECRET_ARGS+=("$s=$v"); ENV_ARGS+=("$k=secretref:$s")
done < "${SECRETS_FILE:-/dev/null}"
if [[ -n "$SECRETS_FILE" && ${#SECRET_ARGS[@]} -eq 0 ]]; then
  echo "SECRETS_FILE has no KEY=VALUE lines" >&2; exit 1
fi

ACR_ID="$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)"
IMAGE="$ACR.azurecr.io/nestock-api:$TAG"

if ! az containerapp show -n "$APP" -g "$RG" >/dev/null 2>&1; then
  # New app: start from a public placeholder image with a system-assigned identity, so the
  # identity exists to receive AcrPull before the first private pull.
  az containerapp create -n "$APP" -g "$RG" --environment "$ENV_NAME" \
    --image mcr.microsoft.com/k8se/quickstart:latest --system-assigned \
    --target-port 8000 --ingress external --min-replicas 1 --max-replicas 1 >/dev/null
fi

PRINCIPAL_ID="$(az containerapp identity assign -n "$APP" -g "$RG" --system-assigned \
  --query principalId -o tsv)"
# Grant AcrPull. An existing assignment is fine. An authorization denial (no right to assign
# roles to a ServicePrincipal) switches to the pull-only token below. Any other failure must
# stop here instead of surfacing later as an unexplained pull error.
PULL_TOKEN="${PULL_TOKEN:-nestock-api-pull}"
PULL_TOKEN_DAYS=30
ACR_AUTH=identity
if ! ROLE_OUT="$(az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" 2>&1)"; then
  if [[ "$ROLE_OUT" == *"already exists"* ]]; then
    :
  elif [[ "$ROLE_OUT" == *AuthorizationFailed* || "$ROLE_OUT" == *"does not have authorization"* ]]; then
    ACR_AUTH=token
    echo "AcrPull assignment denied; using pull-only ACR token $PULL_TOKEN ($PULL_TOKEN_DAYS days)" >&2
  else
    echo "AcrPull assignment failed: $ROLE_OUT" >&2; exit 1
  fi
fi
if [[ "$ACR_AUTH" == token ]]; then
  # Token scope: pull of repository nestock-api only (no push, no other repository).
  # `token create --repository` makes its own scope map. The password is generated fresh on
  # every run (this is also the rotation), kept in a variable, handed to the Container App,
  # and never printed. If any step fails, nothing is set and the script stops.
  az acr token show -n "$PULL_TOKEN" -r "$ACR" -g "$RG" >/dev/null 2>&1 || \
    az acr token create -n "$PULL_TOKEN" -r "$ACR" -g "$RG" --no-passwords \
      --repository nestock-api content/read metadata/read >/dev/null || {
      echo "could not create ACR token $PULL_TOKEN" >&2; exit 1; }
  PULL_PASSWORD="$(az acr token credential generate -n "$PULL_TOKEN" -r "$ACR" -g "$RG" \
    --password1 --expiration-in-days "$PULL_TOKEN_DAYS" --query "passwords[0].value" -o tsv)" || {
    echo "could not generate a password for ACR token $PULL_TOKEN" >&2; exit 1; }
  [[ -n "$PULL_PASSWORD" ]] || { echo "ACR token password is empty" >&2; exit 1; }
  az containerapp registry set -n "$APP" -g "$RG" --server "$ACR.azurecr.io" \
    --username "$PULL_TOKEN" --password "$PULL_PASSWORD" >/dev/null
  unset PULL_PASSWORD
else
  az containerapp registry set -n "$APP" -g "$RG" --server "$ACR.azurecr.io" --identity system >/dev/null
fi
if [[ ${#SECRET_ARGS[@]} -gt 0 ]]; then
  az containerapp secret set -n "$APP" -g "$RG" --secrets "${SECRET_ARGS[@]}" >/dev/null
fi

# `registry set` does not test the pull, and a new role assignment can take minutes to
# propagate. So after each update, wait until the NEW revision is the ready one (checking
# /health alone could be answered by the previous revision). If it never becomes ready,
# update again (a fresh revision) and retry; give up after a few rounds.
for attempt in 1 2 3 4 5 6; do
  az containerapp update -n "$APP" -g "$RG" --image "$IMAGE" \
    --min-replicas 1 --max-replicas 1 ${ENV_ARGS[@]+--set-env-vars "${ENV_ARGS[@]}"} >/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    LATEST="$(az containerapp show -n "$APP" -g "$RG" --query properties.latestRevisionName -o tsv)"
    READY="$(az containerapp show -n "$APP" -g "$RG" --query properties.latestReadyRevisionName -o tsv)"
    if [[ -n "$LATEST" && "$LATEST" == "$READY" ]]; then
      FQDN="$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
      echo "Revision $READY is ready"
      echo "Base URL: https://$FQDN"
      curl -fsS --retry 5 --retry-delay 5 --retry-connrefused "https://$FQDN/health" && echo
      exit 0
    fi
    sleep 10
  done
  echo "attempt $attempt: latest revision not ready (AcrPull may still be propagating); retrying" >&2
  sleep 30
done
echo "deployment did not become ready; check: az containerapp logs show -n $APP -g $RG" >&2
exit 1
