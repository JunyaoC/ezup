#!/usr/bin/env bash
# Deploy the ezupdate Worker without committing any account-specific id.
#
#   export CLOUDFLARE_ACCOUNT_ID=...      # your account
#   export EZUPDATE_D1_ID=...             # from: wrangler d1 create ezupdate
#   ./deploy.sh
#
# Writes wrangler.local.jsonc (gitignored) with the real ids and deploys with
# that, leaving the committed wrangler.jsonc free of anything account-specific.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

: "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID}"
: "${EZUPDATE_D1_ID:?set EZUPDATE_D1_ID (wrangler d1 create ezupdate)}"

python3 - "$CLOUDFLARE_ACCOUNT_ID" "$EZUPDATE_D1_ID" <<'PY'
import json, re, sys
account, database = sys.argv[1], sys.argv[2]
raw = open("wrangler.jsonc").read()
# Strip // comments so the template can stay commented for humans.
stripped = re.sub(r'^\s*//.*$', '', raw, flags=re.M)
config = json.loads(stripped)
config["account_id"] = account
config["d1_databases"][0]["database_id"] = database
with open("wrangler.local.jsonc", "w") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
print("wrote wrangler.local.jsonc")
PY

echo "==> applying D1 schema"
npx wrangler d1 execute ezupdate --remote --file=schema.sql -c wrangler.local.jsonc

echo "==> deploying worker"
npx wrangler deploy -c wrangler.local.jsonc

cat <<'DONE'

Deployed. Next:

  npx wrangler secret put ADMIN_TOKEN -c wrangler.local.jsonc
  # then mint a device token with POST /v1/device

The Worker URL is not committed anywhere: hand it to developers through
EZUPDATE_STORE or their repo's .ez/config.json.
DONE
