#!/bin/bash
# set_etsy_creds.sh -- safely (re)enter Etsy app credentials into .env.
# Run this in YOUR OWN Terminal (interactive). The shared secret is read
# without echo and never printed. Updates only the two credential lines,
# backs up .env first, then pings Etsy to confirm the keystring is active.
#
# Get values from: https://www.etsy.com/developers/your-apps  (your app)
set -u
PROJECT_DIR="/Users/bnieco/PycharmProjects/business_analytics"
cd "$PROJECT_DIR" || { echo "FATAL: no $PROJECT_DIR"; exit 1; }

read -rp  "Paste KEYSTRING (x-api-key, ~24 chars): " KS
read -rsp "Paste SHARED SECRET (hidden): " SS; echo

# strip ALL whitespace
KS="$(printf '%s' "$KS" | tr -d '[:space:]')"
SS="$(printf '%s' "$SS" | tr -d '[:space:]')"

echo "keystring length: ${#KS}    shared_secret length: ${#SS}"
[ "${#KS}" -ne 24 ] && echo "  ^ NOTE: Etsy keystrings are usually 24 chars."

cp .env ".env.bak.$(date +%Y%m%d_%H%M%S)"

python3 - "$KS" "$SS" <<'PY'
import sys
ks, ss = sys.argv[1], sys.argv[2]
p = ".env"
lines = open(p).read().split("\n")
out = []
for ln in lines:
    if ln.startswith("ETSY_API_KEYSTRING="):
        out.append("ETSY_API_KEYSTRING=" + ks)
    elif ln.startswith("ETSY_SHARED_SECRET="):
        out.append("ETSY_SHARED_SECRET=" + ss)
    else:
        out.append(ln)
open(p, "w").write("\n".join(out))
print("updated ETSY_API_KEYSTRING + ETSY_SHARED_SECRET in .env")
PY

set -a; source .env 2>/dev/null; set +a
echo -n "openapi-ping HTTP status: "
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://api.etsy.com/v3/application/openapi-ping" -H "x-api-key: ${ETSY_API_KEYSTRING}:${ETSY_SHARED_SECRET}"
echo "(200 = keystring is live; 403 = not active / wrong key / missing shared secret in x-api-key)"
