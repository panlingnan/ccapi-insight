#!/usr/bin/env bash
#
# refresh_and_deploy.sh — admin-only: regenerate the CloudControl coverage data
# locally and ship it to production.
#
# Secrets are READ FROM THE ENVIRONMENT and never written to disk or git.
# Export your Volcengine credentials first, e.g.:
#
#   export VOLCENGINE_ACCESS_KEY="AKLT..."
#   export VOLCENGINE_SECRET_KEY="...=="
#   ./refresh_and_deploy.sh
#
# What it does:
#   1. Re-fetch all resource types + full OpenAPI surface (fetch + build scripts)
#   2. Commit the regenerated JSON and push to GitHub
#
# Pushing to GitHub auto-deploys to Vercel production
# (https://ccapi-insight.vercel.app/), so no separate deploy step is needed.

set -euo pipefail
cd "$(dirname "$0")"

# --- credentials -----------------------------------------------------------
# The pipeline scripts read ACCESS_KEY / SECRET_KEY; map from VOLCENGINE_* if set.
export ACCESS_KEY="${ACCESS_KEY:-${VOLCENGINE_ACCESS_KEY:-}}"
export SECRET_KEY="${SECRET_KEY:-${VOLCENGINE_SECRET_KEY:-}}"

if [[ -z "${ACCESS_KEY}" || -z "${SECRET_KEY}" ]]; then
  echo "ERROR: set credentials first, e.g."
  echo '  export VOLCENGINE_ACCESS_KEY="AKLT..."'
  echo '  export VOLCENGINE_SECRET_KEY="...=="'
  exit 1
fi

# --- 1. regenerate data ----------------------------------------------------
echo "==> [1/2] Fetching resource types + OpenAPI surface ..."
python3 fetch_ccapi_resourcetypes.py
python3 build_coverage_data.py

# --- 2. commit + push (auto-deploys via Vercel git integration) ------------
# Include excluded-apis.json so admin exclusions persist across deploys.
DATA_FILES=(coverage-data.json ccapi-resourcetype-details.json ccapi-resourcetypes.json excluded-apis.json)
if git diff --quiet -- "${DATA_FILES[@]}"; then
  echo "==> [2/2] No data changes; nothing to deploy."
else
  echo "==> [2/2] Committing + pushing updated data (Vercel auto-deploys) ..."
  git add "${DATA_FILES[@]}"
  git commit -m "data: refresh CloudControl coverage ($(date +%Y-%m-%d))"
  git push
fi

echo "==> Done. Push complete — Vercel will deploy https://ccapi-insight.vercel.app/ shortly."
