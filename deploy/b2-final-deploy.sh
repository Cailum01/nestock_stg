#!/usr/bin/env bash
# B2 final deploy of main revision 4ad5ced to the primary instance (Issue #156).
# Run from macOS Terminal:   bash ~/nestock/deploy/b2-final-deploy.sh
# - Builds from a CLEAN worktree of 4ad5ced, not from your feature branch.
# - Does NOT set SECRETS_FILE, so deploy-aca.sh leaves existing secrets/env vars untouched.
# - Writes a log next to this file so the result can be checked afterwards.
set -euo pipefail

REV=4ad5ced
BASE_URL="https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io"
REPO="$HOME/nestock"
WT="$HOME/nestock-deploy-$REV"
LOG="$REPO/b2-deploy-$REV.log"
stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }

exec > >(tee -a "$LOG") 2>&1
echo "== start $(stamp)"

cd "$REPO"
echo "== your current branch (smoke test is taken from here): $(git branch --show-current) @ $(git rev-parse --short HEAD)"
git fetch origin
FULL="$(git rev-parse --verify "${REV}^{commit}")"
git merge-base --is-ancestor "$FULL" origin/main || { echo "STOP: $REV is not on origin/main"; exit 1; }
echo "== origin/main is now $(git rev-parse --short origin/main); deploying $REV ($FULL)"

if [ ! -d "$WT" ]; then git worktree add --detach "$WT" "$FULL"; fi
cd "$WT"
[ "$(git rev-parse HEAD)" = "$FULL" ] || { echo "STOP: worktree is not at $REV"; exit 1; }
[ -z "$(git status --porcelain -- backend prompts deploy)" ] || { echo "STOP: worktree is dirty"; exit 1; }

az account show >/dev/null 2>&1 || az login
echo "== Azure account in use:"
az account show --query '{name:name, subscription:id, user:user.name}' -o table

printf '\nDeploy %s to nestock-api (restarts the replica)? Type yes to continue: ' "$REV" > /dev/tty
read -r ANSWER < /dev/tty
[ "$ANSWER" = "yes" ] || { echo "== cancelled by user"; exit 1; }

echo "== deploy $(stamp)"
./deploy/deploy-aca.sh

echo "== smoke test (from $REPO) $(stamp)"
python3 "$REPO/deploy/smoke_test.py" --base-url "$BASE_URL"
echo "== DONE $(stamp)"
