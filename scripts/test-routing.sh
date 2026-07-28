#!/usr/bin/env bash
# test-routing.sh — empirically exercise Cloudflare AI Gateway dynamic routing.
#
# Reproduces, against YOUR account: a unified-billing call, a direct Azure BYOK call,
# route CREATE with the exact working schema, the failover proof, the Azure-in-route
# probe, and cleanup. Idempotent and self-cleaning: every route it creates is named
# zz-* and deleted at the end.
#
# WARNING: this creates and deletes routes on the gateway named by $CF_AIG_GATEWAY
# (default: "default"). It only ever touches routes whose name starts with "zz-".
#
# Env (from .env — see .env.example):
#   required: CF_ACCOUNT_ID, CLOUDFLARE_API_TOKEN (AI Gateway edit scope), CF_AIG_TOKEN
#   optional: CF_AIG_BASE_URL, CF_AIG_GATEWAY, FALLBACK_MODEL,
#             AZURE_AI_RESOURCE, AZURE_AI_DEPLOYMENT, AZURE_AI_KEY (steps 2 & 5 only)

# shellcheck source=./load-env.sh
. "$(dirname "$0")/load-env.sh"
set -uo pipefail

: "${CF_ACCOUNT_ID:?CF_ACCOUNT_ID not set — copy .env.example to .env}"
: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN not set — copy .env.example to .env}"
: "${CF_AIG_TOKEN:?CF_AIG_TOKEN not set — copy .env.example to .env}"

GW="${CF_AIG_GATEWAY:-default}"
CF_AIG_BASE_URL="${CF_AIG_BASE_URL:-https://gateway.ai.cloudflare.com/v1/${CF_ACCOUNT_ID}/${GW}}"
REST="https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/ai-gateway"

# A model id that is valid on YOUR account via unified billing. Override in .env.
FALLBACK_MODEL="${FALLBACK_MODEL:-claude-haiku-4-5-20251001}"
BOGUS_MODEL="claude-does-not-exist-9"

PASS=0; FAIL=0
ok(){ echo "  PASS: $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

mgmt(){ curl -s -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H "Content-Type: application/json" "$@"; }

del_test_routes(){
  mgmt "$REST/gateways/$GW/routes?per_page=100" \
    | python3 -c 'import sys,json;[print(r["id"],r["name"]) for r in json.load(sys.stdin)["data"]["routes"]]' \
    | while read -r id name; do
        [[ "$name" == zz-* ]] && { mgmt -X DELETE "$REST/gateways/$GW/routes/$id" >/dev/null; echo "  cleaned $name"; }
      done
}

echo "== 0. Pre-clean any leftover zz-* test routes on gateway '$GW' =="
del_test_routes

echo "== 1. UNIFIED call (anthropic via unified billing) =="
code=$(curl -s -o /tmp/agt_u.json -w "%{http_code}" "$CF_AIG_BASE_URL/compat/chat/completions" \
  -H "cf-aig-authorization: Bearer ${CF_AIG_TOKEN}" -H "Content-Type: application/json" \
  -d "{\"model\":\"anthropic/$FALLBACK_MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"say hi\"}]}")
# A 402 here means the gateway is missing `authentication: true` and/or a shared store_id.
[[ "$code" == 200 ]] && ok "unified 200" || no "unified got $code (402 => see the store_id section in the README)"

echo "== 2. AZURE BYOK call (direct provider path — the WORKING way to reach Azure) =="
if [[ -n "${AZURE_AI_KEY:-}" && -n "${AZURE_AI_RESOURCE:-}" && -n "${AZURE_AI_DEPLOYMENT:-}" ]]; then
  code=$(curl -s -o /tmp/agt_az.json -w "%{http_code}" \
    "$CF_AIG_BASE_URL/azure-openai/${AZURE_AI_RESOURCE}/${AZURE_AI_DEPLOYMENT}/chat/completions?api-version=2024-10-21" \
    -H "cf-aig-authorization: Bearer ${CF_AIG_TOKEN}" -H "api-key: ${AZURE_AI_KEY}" -H "Content-Type: application/json" \
    -d '{"max_completion_tokens":50,"messages":[{"role":"user","content":"hi"}]}')
  [[ "$code" == 200 ]] && ok "azure BYOK direct 200" || no "azure direct got $code"
else
  echo "  SKIP: AZURE_AI_RESOURCE / AZURE_AI_DEPLOYMENT / AZURE_AI_KEY not set"
fi

echo "== 3. ROUTE CREATE — exact working dynamic-route schema =="
# Schema discovered empirically: start.outputs.next.elementId ; a model element needs
# properties{provider,model,timeout,retries} + outputs.success.elementId + outputs.fallback.elementId ;
# the terminal 'end' element needs outputs:{} . Element types: start|conditional|percentage|rate|model|end.
create_route(){ # $1=route name  $2=primary model id (invalid => triggers failover)
  mgmt -X POST "$REST/gateways/$GW/routes" -d "{
    \"name\":\"$1\",
    \"elements\":[
      {\"id\":\"start\",\"type\":\"start\",\"outputs\":{\"next\":{\"elementId\":\"bad\"}}},
      {\"id\":\"bad\",\"type\":\"model\",\"properties\":{\"provider\":\"anthropic\",\"model\":\"$2\",\"timeout\":30000,\"retries\":0},\"outputs\":{\"success\":{\"elementId\":\"end\"},\"fallback\":{\"elementId\":\"good\"}}},
      {\"id\":\"good\",\"type\":\"model\",\"properties\":{\"provider\":\"anthropic\",\"model\":\"$FALLBACK_MODEL\",\"timeout\":30000,\"retries\":0},\"outputs\":{\"success\":{\"elementId\":\"end\"},\"fallback\":{\"elementId\":\"end\"}}},
      {\"id\":\"end\",\"type\":\"end\",\"outputs\":{}}
    ]}"
}
res=$(create_route "zz-test-failover" "$BOGUS_MODEL")
echo "$res" | python3 -c 'import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get("success") else 1)' \
  && ok "route created (schema valid)" || no "route create failed: $res"

# bad-only control route (no real fallback) to prove the primary genuinely fails
mgmt -X POST "$REST/gateways/$GW/routes" -d "{
  \"name\":\"zz-test-badonly\",
  \"elements\":[
    {\"id\":\"start\",\"type\":\"start\",\"outputs\":{\"next\":{\"elementId\":\"bad\"}}},
    {\"id\":\"bad\",\"type\":\"model\",\"properties\":{\"provider\":\"anthropic\",\"model\":\"$BOGUS_MODEL\",\"timeout\":30000,\"retries\":0},\"outputs\":{\"success\":{\"elementId\":\"end\"},\"fallback\":{\"elementId\":\"end\"}}},
    {\"id\":\"end\",\"type\":\"end\",\"outputs\":{}}
  ]}" >/dev/null
sleep 1

echo "== 4. FAILOVER PROOF — invoke dynamic/<route> via the compat endpoint =="
# NOTE: cf-aig-step stays 0 for dynamic routes — it does NOT increment on failover.
# So failover is proven functionally: the failover route succeeds with the FALLBACK model
# while the bad-only control errors on the same invalid primary.
fo_code=$(curl -s -o /tmp/agt_fo.json -w "%{http_code}" "$CF_AIG_BASE_URL/compat/chat/completions" \
  -H "cf-aig-authorization: Bearer ${CF_AIG_TOKEN}" -H "Content-Type: application/json" \
  -d '{"model":"dynamic/zz-test-failover","max_tokens":16,"messages":[{"role":"user","content":"say hi"}]}')
fo_model=$(python3 -c 'import json;print(json.load(open("/tmp/agt_fo.json")).get("model",""))' 2>/dev/null)
bo_code=$(curl -s -o /tmp/agt_bo.json -w "%{http_code}" "$CF_AIG_BASE_URL/compat/chat/completions" \
  -H "cf-aig-authorization: Bearer ${CF_AIG_TOKEN}" -H "Content-Type: application/json" \
  -d '{"model":"dynamic/zz-test-badonly","max_tokens":16,"messages":[{"role":"user","content":"say hi"}]}')
echo "  failover route -> HTTP $fo_code, model=$fo_model ; bad-only control -> HTTP $bo_code"
if [[ "$fo_code" == 200 && "$fo_model" == "$FALLBACK_MODEL" && "$bo_code" != 200 ]]; then
  ok "FAILOVER FIRED (failover=200 on the fallback model, bad-only=$bo_code)"
else
  no "failover not proven (fo=$fo_code/$fo_model bo=$bo_code)"
fi

echo "== 5. CRUX — can a route include an Azure step? =="
mgmt -X POST "$REST/gateways/$GW/routes" -d "{
  \"name\":\"zz-test-azure\",
  \"elements\":[
    {\"id\":\"start\",\"type\":\"start\",\"outputs\":{\"next\":{\"elementId\":\"m1\"}}},
    {\"id\":\"m1\",\"type\":\"model\",\"properties\":{\"provider\":\"azure-openai\",\"model\":\"${AZURE_AI_DEPLOYMENT:-any-deployment}\",\"timeout\":30000,\"retries\":0},\"outputs\":{\"success\":{\"elementId\":\"end\"},\"fallback\":{\"elementId\":\"end\"}}},
    {\"id\":\"end\",\"type\":\"end\",\"outputs\":{}}
  ]}" >/dev/null
sleep 1
az_code=$(curl -s -o /tmp/agt_azr.json -w "%{http_code}" "$CF_AIG_BASE_URL/compat/chat/completions" \
  -H "cf-aig-authorization: Bearer ${CF_AIG_TOKEN}" -H "api-key: ${AZURE_AI_KEY:-}" -H "Content-Type: application/json" \
  -d '{"model":"dynamic/zz-test-azure","max_completion_tokens":50,"messages":[{"role":"user","content":"hi"}]}')
echo "  azure-in-route invoke -> HTTP $az_code (expected 500: the route schema has no Azure resource / api-version / BYOK field)"
[[ "$az_code" == 500 ]] && ok "azure-in-route NOT functional (500) — verdict confirmed" \
                        || echo "  NOTE: azure route returned $az_code (investigate if != 500)"

echo "== 6. Cleanup — delete every zz-* route =="
del_test_routes
remaining=$(mgmt "$REST/gateways/$GW/routes?per_page=100" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"]["routes"]))')
echo "  routes remaining on '$GW': $remaining"

echo "== RESULT: $PASS passed, $FAIL failed =="
exit $FAIL
