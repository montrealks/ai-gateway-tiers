# ai-gateway-tiers

**Stop naming models in your application code.** Ask for a *quality tier* instead — `dynamic/low`, `dynamic/high` — and let Cloudflare AI Gateway decide which model answers, route it to a free provider first, and fail over to a paid one automatically when the free quota runs out.

No proxy service. No Worker. No SDK. This is ~200 lines of configuration against the Cloudflare AI Gateway REST API plus an OpenAI-compatible endpoint your existing client library already knows how to talk to.

---

## The problem

Two problems, actually, and they compound.

**1. Model lock-in leaks into every codebase.** `"model": "claude-sonnet-5"` ends up hardcoded across a dozen files, in three languages, in five repos. A new model ships, a model id is deprecated, a provider has an outage — and now you're doing a find-and-replace across your whole estate to react to it. Model choice is an *operational* decision that has no business being a *source-code* decision.

**2. Free tiers are real money, but you can't rely on them.** Google AI Studio gives away a genuinely useful daily quota of Gemini. It's free until it isn't — you hit a rate limit, you get a `429`, and your feature breaks. So most people never route production traffic through a free tier at all, and pay full price for work that a free model would have handled fine.

The fix for both is the same: put an indirection layer between "my app wants an answer" and "which model produced it."

## The tier concept

An app declares *what kind of thinking it needs*, not *which vendor's weights*:

| Tier | For | Typical work |
|---|---|---|
| `low` | cheap and fast | classify, tag, extract fields, spam-score, summarize |
| `high` | flagship reasoning | structured extraction, judgment calls, multi-step analysis |
| `code` *(optional)* | top-end | agentic coding, hard reasoning where quality beats cost |

Application code says `model: "dynamic/low"` and never changes again. Which model that resolves to is gateway configuration — editable from a terminal, live, without a deploy.

Two tiers cover almost everything a user-facing product does. Resist adding more.

## The arbitrage: free primary, paid failover

Each tier is a dynamic route with two steps:

```
start ──▶ primary: google-ai-studio/gemini-*   (BYOK — bills YOUR free quota)
             │ success ──────────────────────▶ end
             │ fallback (429 / error / timeout)
             ▼
          fallback: anthropic/claude-*         (Cloudflare unified billing — paid)
             │ success ──────────────────────▶ end
             ▼
            end
```

The primary is your own Google AI Studio key, stored **inside the gateway** as a BYOK provider key. Calls through it bill against Google's free tier, not against Cloudflare credits. When the free tier throttles you — `429` — the route silently steps to a paid Anthropic model and the request still returns `200`. Your user never sees a failure; you just paid a few cents for that one request instead of zero.

Net effect: **you pay only for the overflow.** Steady-state cost tracks the *excess* over the free quota, not total volume. Meanwhile the fallback gives you genuine cross-provider redundancy — a Google outage degrades your bill, not your uptime.

Critically, **no API key ever leaves your app**. The client sends only a gateway token. Provider credentials live in the gateway's key store.

---

## Quickstart

### 0. What you need

- A Cloudflare account with AI Gateway enabled.
- A Cloudflare API token with AI Gateway **edit** scope (management only).
- An AI Gateway authorization token (the runtime, app-facing bearer).
- A Google AI Studio API key (the free primary), added to the gateway as a provider key.
- Cloudflare **Unified Billing** enabled, for the paid fallback.

```bash
cp .env.example .env   # then fill it in
```

### 1. Point any OpenAI-compatible client at the gateway

```bash
curl -s https://gateway.ai.cloudflare.com/v1/$CF_ACCOUNT_ID/tiers/compat/chat/completions \
  -H "cf-aig-authorization: Bearer $CF_AIG_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"dynamic/low","max_tokens":16,
       "messages":[{"role":"user","content":"reply with: pong"}]}' -D -
```

The `cf-aig-model` response header tells you which model actually answered — that's how you observe failover.

### 2. Verify all your tiers

```bash
./scripts/tiers-test.sh          # asserts every tier returns 200, prints cf-aig-model
python3 scripts/stress-test.py 400 30   # concurrent load; shows the model mix under pressure
```

---

## Provisioning it yourself (AI Gateway REST API)

Everything below is `POST`/`GET`/`DELETE` against
`https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/ai-gateway`
with `Authorization: Bearer $CLOUDFLARE_API_TOKEN`.

### Step 1 — read your default gateway's `store_id`

Do this **first**. You need this value in step 2, and skipping it is the single most common way to end up with a gateway that returns `402` forever (see the reverse-engineering section below).

```bash
curl -s "$REST/gateways/default" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | jq -r '.result.store_id'
```

### Step 2 — create the `tiers` gateway

All of these fields are required by the API even though most are irrelevant to routing:

```bash
curl -s -X POST "$REST/gateways" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "id": "tiers",
    "cache_ttl": 0,
    "cache_invalidate_on_update": false,
    "collect_logs": true,
    "rate_limiting_interval": 0,
    "rate_limiting_limit": 0,
    "rate_limiting_technique": "sliding",
    "authentication": true,
    "store_id": "<STORE_ID_FROM_STEP_1>"
  }'
```

### Step 3 — add the free provider key (BYOK)

Add your Google AI Studio key to the gateway's key store, scoped to this gateway and the `google-ai-studio` provider. This is what makes the primary step bill Google's free tier instead of Cloudflare credits. (Easiest in the dashboard: *AI Gateway → tiers → Provider Keys*.)

### Step 4 — create one route per tier

```bash
curl -s -X POST "$REST/gateways/tiers/routes" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "low",
    "elements": [
      {"id":"start","type":"start","outputs":{"next":{"elementId":"primary"}}},
      {"id":"primary","type":"model",
       "properties":{"provider":"google-ai-studio","model":"gemini-3.6-flash","timeout":30000,"retries":0},
       "outputs":{"success":{"elementId":"end"},"fallback":{"elementId":"secondary"}}},
      {"id":"secondary","type":"model",
       "properties":{"provider":"anthropic","model":"claude-haiku-4-5-20251001","timeout":30000,"retries":0},
       "outputs":{"success":{"elementId":"end"},"fallback":{"elementId":"end"}}},
      {"id":"end","type":"end","outputs":{}}
    ]}'
```

Repeat for `high` (and `code`, if you want it) using the models in [`tiers.json`](tiers.json).

**Route schema gotchas**, all discovered by trial and error against the live API — the docs don't spell these out:

- `elementId` is **camelCase**. Snake case is rejected with an unhelpful error.
- Element types are `start | conditional | percentage | rate | model | end`.
- Every `model` element **must** carry `timeout` (ms) and `retries` — they are not optional.
- A terminal `end` element is mandatory and its `outputs` must be present as `{}`.
- `fallback` fires on error, timeout, *and* rate limit — which is exactly what makes the free-tier arbitrage work.

Routes are invoked as `"model": "dynamic/<route-name>"`. Listing and deleting use the same path: `GET`/`DELETE $REST/gateways/tiers/routes[/<id>]`.

---

## The call surface

One OpenAI-compatible endpoint. Any client library that lets you override `base_url` works unmodified.

- **Base:** `https://gateway.ai.cloudflare.com/v1/<CF_ACCOUNT_ID>/tiers`
- **Endpoint:** `POST {base}/compat/chat/completions`
- **Model field:** `"model": "dynamic/<tier>"`
- **Auth:** `cf-aig-authorization: Bearer <CF_AIG_TOKEN>`
- **Attribution (optional):** `cf-aig-metadata: {"project":"<app>"}` — tags every request so gateway logs break down cost per app.
- **Observability:** the `cf-aig-model` response header names the model that answered.

Never hardcode the token. Read it from the environment.

### Python

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=f"https://gateway.ai.cloudflare.com/v1/{os.environ['CF_ACCOUNT_ID']}/tiers/compat",
    api_key="unused",  # real auth is the gateway header below
    default_headers={
        "cf-aig-authorization": f"Bearer {os.environ['CF_AIG_TOKEN']}",
        "cf-aig-metadata": '{"project":"my-app"}',
    },
)

r = client.chat.completions.create(
    model="dynamic/low",
    messages=[{"role": "user", "content": "Classify this ticket: ..."}],
)
print(r.choices[0].message.content)
```

### TypeScript (fetch — works in Node, Bun, and Cloudflare Workers)

```ts
const res = await fetch(
  `https://gateway.ai.cloudflare.com/v1/${env.CF_ACCOUNT_ID}/tiers/compat/chat/completions`,
  {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "cf-aig-authorization": `Bearer ${env.CF_AIG_TOKEN}`,
      "cf-aig-metadata": JSON.stringify({ project: "my-app" }),
    },
    body: JSON.stringify({
      model: "dynamic/high",
      messages: [{ role: "user", content: "Extract the invoice fields as JSON: ..." }],
    }),
  },
);

const whichModel = res.headers.get("cf-aig-model"); // e.g. gemini-3.6-flash, or the paid fallback
const data = await res.json();
```

### PHP (WordPress HTTP API — the same shape works with Guzzle or plain cURL)

```php
$res = wp_remote_post(
  sprintf('https://gateway.ai.cloudflare.com/v1/%s/tiers/compat/chat/completions', getenv('CF_ACCOUNT_ID')),
  [
    'headers' => [
      'Content-Type'         => 'application/json',
      'cf-aig-authorization' => 'Bearer ' . getenv('CF_AIG_TOKEN'),
      'cf-aig-metadata'      => wp_json_encode(['project' => 'my-app']),
    ],
    'body' => wp_json_encode([
      'model'    => 'dynamic/low',
      'messages' => [['role' => 'user', 'content' => $prompt]],
    ]),
    'timeout' => 60,
  ]
);

$model = wp_remote_retrieve_header($res, 'cf-aig-model');
$body  = json_decode(wp_remote_retrieve_body($res), true);
```

---

## Undocumented behavior we reverse-engineered

### The `store_id` / `402` trap

**Symptom.** You create a second, non-default AI Gateway. Unified-billing (keyless) calls that work perfectly on the `default` gateway now fail on the new one with:

```
402  "Gateway authentication is required to use unified billing"
```

The message is misleading. You *have* set `authentication: true`. You *are* sending a valid `cf-aig-authorization` bearer. It still 402s, indefinitely, on every model and every provider.

**Cause.** Unified billing isn't bound to the gateway — it's bound to the gateway's **key store**, identified by `store_id`. A newly created gateway gets its own empty store, which carries no unified-billing entitlement. The entitlement lives on the store attached to your account's `default` gateway.

**Fix.** Set the new gateway's `store_id` to the *same* value as the default gateway's:

```bash
STORE_ID=$(curl -s "$REST/gateways/default" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | jq -r '.result.store_id')

curl -s -X PATCH "$REST/gateways/tiers" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"authentication\": true, \"store_id\": \"$STORE_ID\"}"
```

Both `authentication: true` **and** the shared `store_id` are required. Either alone gives you the 402. As of this writing, none of this appears in Cloudflare's AI Gateway documentation, and `store_id` isn't presented as something you're meant to set at all.

### Azure OpenAI cannot be a route step

`azure-openai` is accepted as a `provider` value when you *create* a dynamic route — the API returns success. At *invoke* time it returns `500`.

The reason is structural: Azure needs a resource name, an `api-version` query parameter, and an `api-key` header, and the route element schema has fields for none of them. The route can't construct a valid Azure request. Azure works fine through the gateway's **direct provider path** (`{base}/azure-openai/<resource>/<deployment>/chat/completions?api-version=...`) — just never inside a route.

`scripts/test-routing.sh` step 5 probes this and asserts the `500`.

### `cf-aig-step` doesn't increment on dynamic routes

The obvious way to detect failover would be the `cf-aig-step` response header. On the account we tested it stays `0` regardless of which step served the request. Don't build alerting on it. **Use `cf-aig-model` instead** — it accurately names the model that answered, which is what you actually want anyway.

### How failover was actually proven

Not by reading docs. By deploying a route whose primary was a deliberately non-existent model id, plus a "bad-only" control route with the same bogus primary and *no* fallback:

- failover route → **HTTP 200**, `cf-aig-model` = the secondary model
- bad-only control → **non-200**

That pair rules out the alternative explanation (that the bogus model id was being silently accepted). Both routes were deleted afterward. `scripts/test-routing.sh` reproduces the whole sequence and cleans up after itself.

---

## Limitations — read before adopting

- **Cloudflare-specific.** This is a configuration pattern for one vendor's product, not a portable library. There's no abstraction layer here to swap out.
- **No cost-based or latency-based routing.** The route fires on error, timeout, or rate limit. It will not pick the cheaper model because a prompt looks easy, nor the faster one because a user is waiting.
- **The tier boundary is a human judgment call.** Nothing measures whether `low` is actually good enough for a given prompt. You decide, and you find out you were wrong from output quality, not from a metric.
- **Model ids drift.** The ids in `tiers.json` were valid on the date they were verified. Providers deprecate ids on their own schedule; a stale `tiers.json` fails at runtime, not at deploy. Re-run `scripts/tiers-test.sh` periodically — that's the entire point of it.
- **Free-tier quotas are a moving target.** The economics rest on a free tier that the provider can shrink, reprice, or withdraw with little notice. The failover means you degrade to *paying*, not to *breaking* — but budget for the possibility that the primary stops being free.
- **Failover costs latency.** A failed primary burns its timeout before the fallback starts. With a 30s timeout, a hung primary means a ~30s floor on that request. Tune `timeout` down if you're on a user-facing path.
- **`store_id` sharing is undocumented, and therefore unstable.** Cloudflare didn't document it, which means they didn't promise it. It works today; it isn't a contract.
- **Streaming and tool-calling were not exercised.** Verification covered non-streaming chat completions only. Behavior of a mid-stream failover, in particular, is untested — assume it's rough.
- **No retries configured.** Every route step ships `retries: 0` deliberately, so a failing primary moves to the fallback immediately rather than burning time retrying. Raise it if your primary fails transiently more often than it fails hard.
- **Verified on one account.** Some of this — the `cf-aig-step` behavior especially — may be account- or plan-specific.

## Files

```
tiers.json                 tier → model mapping, the source of truth
scripts/tiers-test.sh      smoke test: every tier returns 200, prints cf-aig-model
scripts/test-routing.sh    exploratory harness: route CRUD, failover proof, Azure probe, cleanup
scripts/stress-test.py     concurrent load test; shows the model mix (and failover) under pressure
.env.example               required environment variables
```

## License

MIT — see [LICENSE](LICENSE).
