#!/usr/bin/env bash
# tiers-test.sh — assert every dynamic-route tier returns 200 and report which model answered.
# No secrets are hardcoded; values come from .env (see .env.example) or the environment.
#
# Usage:  ./scripts/tiers-test.sh [tier ...]     (default: low high)

# shellcheck source=./load-env.sh
. "$(dirname "$0")/load-env.sh"
set -eu

: "${CF_ACCOUNT_ID:?CF_ACCOUNT_ID not set — copy .env.example to .env}"
: "${CF_AIG_TOKEN:?CF_AIG_TOKEN not set — copy .env.example to .env}"

GATEWAY="${CF_AIG_GATEWAY:-tiers}"
BASE="https://gateway.ai.cloudflare.com/v1/${CF_ACCOUNT_ID}/${GATEWAY}"

if [ "$#" -gt 0 ]; then
  TIERS=("$@")
else
  TIERS=(low high)
fi

FAIL=0
echo "AI Gateway tiers smoke test — gateway '${GATEWAY}'"
echo "---------------------------------------------"
for t in "${TIERS[@]}"; do
  hdrs="$(mktemp)"
  code="$(curl -s -D "$hdrs" -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/compat/chat/completions" \
    -H "cf-aig-authorization: Bearer ${CF_AIG_TOKEN}" \
    -H "Content-Type: application/json" \
    -H 'cf-aig-metadata: {"project":"tiers-test"}' \
    -d "{\"model\":\"dynamic/${t}\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"reply with: pong\"}]}")"
  model="$(grep -i '^cf-aig-model:' "$hdrs" | tr -d '\r' | sed 's/^[^:]*: *//')"
  rm -f "$hdrs"
  if [ "$code" = "200" ]; then
    printf 'PASS  dynamic/%-5s HTTP %s  cf-aig-model: %s\n' "$t" "$code" "${model:-?}"
  else
    printf 'FAIL  dynamic/%-5s HTTP %s\n' "$t" "$code"
    FAIL=1
  fi
done

echo "---------------------------------------------"
if [ "$FAIL" -eq 0 ]; then
  echo "OK: all ${#TIERS[@]} tiers returned 200."
  exit 0
fi
echo "FAILURE: one or more tiers did not return 200."
exit 1
