# ai-gateway-tiers

**Ask for a capability tier, never a model.** Every app names `low`, `high`, `offload`, `code` or `embed`; this repo owns what those resolve to.

Azure leads every tier — Microsoft-for-Startups credits are free until ~2026-09-21, and there is more credit than can be spent before then. Each tier falls back to a *second Azure model* before it will consider anything that costs money.

```
tiers.json         source of truth — the chains, the policy, the stored keys
client/aigw.py     the client every app installs
```

## Use it

```bash
pip install -e ./client
```

```python
from aigw import chat, embed          # async: achat, aembed

text = chat("low", "Classify this as spam or not: ...")
cfg  = chat("low", prompt, json_mode=True)          # -> parsed dict
out  = chat("high", prompt, images=[jpeg_b64])      # vision
vec  = embed("a sentence")                          # 1536 dims
```

Needs `CF_ACCOUNT_ID`, `CF_AIG_TOKEN` and `AZURE_RESOURCE` — see `.env.example`. No provider key: they're stored in the gateway, so calls go keyless.

Pass `project="<app>"` on every call. It becomes `cf-aig-metadata` and is what makes spend traceable per app in the gateway logs.

Verify a working setup, and see which provider actually answered:

```bash
python3 scripts/selftest.py            # every tier, plus json_mode, vision, embeddings
python3 scripts/probe_text.py          # latency + correctness across deployments
python3 scripts/stress.py 120 30       # concurrency, attribution, and failover
```

`stress.py` deliberately breaks the first step of a chain and asserts a later one still answers —
failover is the property the whole design rests on, and it's invisible in normal operation.

## The tiers

| Tier | Purpose | Chain |
|---|---|---|
| `low` | classify, tag, extract, short generation | Azure gpt-5.6-luna → Azure gpt-5.4 → Gemini free → Claude Haiku |
| `high` | reasoning, structured extraction | Azure gpt-5.4 → Azure gpt-5.6-terra → Gemini free → Claude Sonnet |
| `offload` | bulk / deterministic / dev work | Azure DeepSeek-V4-Flash → Azure DeepSeek-V4-Pro → Azure gpt-5.4 |
| `code` | code-heavy / agentic | Azure gpt-5.4 → Azure DeepSeek-V4-Flash |
| `embed` | embeddings, 1536-dim | Azure text-embedding-3-small |

`offload` and `code` are **Azure-only by design** — they fail rather than escalate to a paid provider.

`Kimi-K2.7-Code` is deliberately absent: its deployment capacity is 100 against 500 for `gpt-5.4` and `DeepSeek-V4-Pro`, so it saturates on real workloads and returns `finish_reason: length` with empty content.

## Profiles

A tier says *what capability you need*. A profile says *which free pool should pay for it*. A profile only **reorders** a tier's existing chain — it can never introduce a model the tier doesn't already list, so capability stays a property of the tier alone.

```python
chat("low", prompt)                      # default — Azure first
chat("low", prompt, profile="client")    # Gemini free first, Azure second
```

| Profile | Order | Why |
|---|---|---|
| `default` | Azure → Google → Anthropic | Azure credits are free but **finite and expire ~2026-09-21**. Spend them first; unspent credit is worth nothing after that date. |
| `client` | Google → Azure → Anthropic | The Google free tier is **perpetual and resets daily**. Production client sites must keep working past September without generating a bill, so they lean on the renewing pool. Gemini flash is also the lowest-latency option measured here, and this traffic is user-visible. |

Use `client` for customer-facing microtasks on client sites — tag suggestion, short content suggestion. **Do not use it for private or client-confidential data:** free-tier inputs may be used to train Google's models. Anything sensitive belongs on the default profile, which leads with Azure.

The `client` profile's premise is that Google *cannot* bill you. That is not a property of the key — it's a property of the key's Google Cloud project, and it flips silently the moment a billing account is attached for any reason, including an unrelated API like Maps. So it's checked, not documented:

```bash
python3 scripts/verify_free_tier.py    # selftest runs this too
```

## How it works

The client POSTs an ordered **array** of attempts to the gateway's universal endpoint at `…/v1/{account}/tiers`; the gateway returns the first that succeeds.

```json
[ {"provider":"azure-openai",
   "endpoint":"$AZURE_RESOURCE/gpt-5.6-luna/chat/completions?api-version=2024-10-21",
   "headers":{"Content-Type":"application/json"},
   "query":{"messages":[]}},
  {"provider":"anthropic","endpoint":"v1/messages","headers":{},"query":{}} ]
```

`query` is the request **body**, not URL params — anything that must be a URL param goes inside `endpoint`. The response comes back in the winning provider's native shape; `aigw` normalises it.

This uses the universal endpoint rather than *dynamic routes* for one reason: a dynamic route's model element has fields for `provider`/`model` only, with nowhere to put Azure's resource name or api-version. **Azure cannot be a dynamic-route step** — such a step never reaches the provider and logs nothing, while the route still returns 200 off the fallback, so the dead step is easy to miss. Since Azure-first is the entire policy, the chain has to live in the request.

Azure throttling is per-deployment, so each tier's second step is a different Azure model — that rescues a 429 at zero cost. All deployments share one resource, so an account-level failure takes the whole Azure section with it; that is what the Google and Cloudflare steps exist for.

## Choosing models

**Cost is not a selection criterion while the credits last.** There is more credit than can be spent
before they expire, so pick on capability, latency and quota headroom — never on price. A vendor
price cut is not a reason to swap.

**Don't chase releases.** A swap is one edit to `tiers.json`, so there's no advantage in being early
and no cost to being late. Re-evaluate at exactly two moments:

1. **When the credits expire** (~2026-09-21) — that's when price starts to matter at all, and the
   chain's economics invert: the paid tail becomes the expensive part rather than the safety net.
   Two candidates already in the Azure catalog would make that tail cheap — `gpt-5.6-luna`
   (~$0.20/$1.20 per 1M) and `DeepSeek-V4-Flash` (~$0.14/$0.28), both close enough to free-tier
   economics to sit above Cloudflare unified billing.
2. **When something shows a measured win on real workloads.** Vendor benchmarks are not evidence.
   Run the comparison against pages, prompts and tasks the apps actually send.

### Azure catalog worth knowing about

- **`gpt-5.6` ships as `sol` / `terra` / `luna`** — durable *capability tiers*, not sizes. They replace
  the mini/nano suffixes and advance on their own cadence, which maps straight onto `low`/`high` here.
  Luna's deprecation runs to 2028-01-11 against 2027-07-09 for sol and terra, so luna is the one
  positioned as the durable high-volume workhorse.
- **`DeepSeek-V4-Flash` looks like it beats `DeepSeek-V4-Pro`** on agentic and coding work — but those
  results come from DeepSeek's 2026-07-31 release, and the gains are post-training only. The Azure
  build is `2026-04-23`, three months earlier. Assume Azure serves the weaker preview weights until
  measured otherwise; `V4-Pro` on Azure carries the same build date.
- **DeepSeek V4 runs thinking mode by default**, billed at the output rate while invisible in the
  response. Any estimate counting only visible output tokens understates real spend.

## Rules

**Never send `max_tokens`.** Every gpt-5.x rejects it (`400 Unsupported parameter`) because it wants `max_completion_tokens`, and the compat layer does not translate. Omitting it works on every provider and is the only form that survives failover.

**BYOK does not mean free.** Storing a provider key moves spend onto *your* account with that provider; it bills whatever that account bills. Confirm an account is genuinely free-tier before treating a step as free.

**A Google AI Studio key is free only while its Cloud project has no billing account.** Attaching billing — even for an unrelated API like Maps — silently moves the key to the paid tier, where free quota does not apply and no 429 is emitted. Keep the AI Studio key in a project with no billing account, so it cannot be charged at all:

```bash
gcloud billing projects describe <project-id>   # billingEnabled must be false
```

**Check the project ID, never the display name.** Display names are not unique, and AI Studio names each new project after whatever you were working on at the time — so two projects with the same name and opposite billing status is easy to create by accident. In July 2026 that cost $74.30: a batch job ran through a BYOK key belonging to a billed project, and the only signal was the invoice. No 429, no warning, nothing.

The unbilled project here is now named `gemini-free-tier` (ID `gen-lang-client-0291098513`) precisely so it can't be confused with the billed app project again. Verify by ID anyway.

**The gateway's `cost` field cannot prove anything is free.** It's a list-price estimate that ignores hidden reasoning tokens and can understate real spend by ~8x.

## Calling it from other languages

The client is Python, but the wire format is just an ordered JSON array — any language can post it.
Read the `cf-aig-model` response header to see which link answered.

### TypeScript (fetch — Node, Bun, Cloudflare Workers)

```ts
const chain = [
  { provider: "azure-openai",
    endpoint: `${env.AZURE_RESOURCE}/gpt-5.6-luna/chat/completions?api-version=2024-10-21`,
    headers: { "Content-Type": "application/json" },
    query:   { messages: [{ role: "user", content: prompt }] } },
  { provider: "anthropic",
    endpoint: "v1/messages",
    headers: { "Content-Type": "application/json", "anthropic-version": "2023-06-01" },
    query:   { model: "claude-haiku-4-5-20251001", max_tokens: 4096,
               messages: [{ role: "user", content: prompt }] } },
];

const res = await fetch(`https://gateway.ai.cloudflare.com/v1/${env.CF_ACCOUNT_ID}/tiers`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "cf-aig-authorization": `Bearer ${env.CF_AIG_TOKEN}`,
    "cf-aig-metadata": JSON.stringify({ project: "my-app" }),
  },
  body: JSON.stringify(chain),
});

const whichModel = res.headers.get("cf-aig-model");
```

### PHP (WordPress HTTP API — same shape with Guzzle or cURL)

```php
$res = wp_remote_post(
  sprintf('https://gateway.ai.cloudflare.com/v1/%s/tiers', getenv('CF_ACCOUNT_ID')),
  [
    'headers' => [
      'Content-Type'         => 'application/json',
      'cf-aig-authorization' => 'Bearer ' . getenv('CF_AIG_TOKEN'),
      'cf-aig-metadata'      => wp_json_encode(['project' => 'my-app']),
    ],
    'body' => wp_json_encode([[
      'provider' => 'azure-openai',
      'endpoint' => getenv('AZURE_RESOURCE') . '/gpt-5.6-luna/chat/completions?api-version=2024-10-21',
      'headers'  => ['Content-Type' => 'application/json'],
      'query'    => ['messages' => [['role' => 'user', 'content' => $prompt]]],
    ]]),
    'timeout' => 60,
  ]
);

$model = wp_remote_retrieve_header($res, 'cf-aig-model');
```

## Undocumented gateway behaviour

Things the docs don't tell you, found by testing against a live account.

**Azure cannot be a dynamic-route step.** A route's model element has fields for `provider` and
`model` only — nowhere for Azure's resource name or api-version. Such a step never reaches the
provider and logs nothing, while the route still returns 200 off its fallback, so the dead step is
easy to miss. This is why the chain lives in the request via the universal endpoint.

**`query` is the request body.** On the universal endpoint each element's `query` field carries the
payload, not URL parameters. Anything that must be a URL parameter goes inside the `endpoint` string.

**`dynamic/<tier>` resolves only on the gateway that owns the route.** Point a base URL at a different
gateway and you get `400 internalCode 2005` with no log entry — indistinguishable from a broken tier,
and it will send you debugging the wrong thing.

**Stored provider keys bind by name alone.** There is no separate provider-configuration call, despite
what the docs imply. Create a Secrets Store secret named `{gateway_id}_{provider_slug}_{alias}` scoped
`ai_gateway` and that gateway goes keyless within seconds.

**Unified billing is per-gateway and not predicted by `store_id`.** One gateway with an empty
`store_id` serves unified fine while another with an empty `store_id` returns `401 x-api-key header is
required`. If a gateway asks for a provider key, unified isn't enabled on it — check that gateway
rather than copying another's `store_id`.

**`cf-aig-step` does not increment.** Read `cf-aig-model` to find out which link answered.

**Platform BYOK beats unified.** If a gateway holds a stored key for a provider, it is used instead of
Cloudflare's, even for a unified-eligible provider.

## Specialist providers — not tiers

Reached directly for the one job each is good at; don't build general text generation on them.

- **Groq** — free, but only ~8,000 tokens/minute, far too tight for bulk. Speech-to-text `whisper-large-v3-turbo`, TTS `canopylabs/orpheus-v1-english`, moderation `meta-llama/llama-prompt-guard-2-86m`.
- **Cerebras** — free and very generous (1M tokens/min, 2B/day). `gpt-oss-120b`, `zai-glm-4.7`. Reserve capacity for large, non-latency-critical text batches when Azure is throttled.

## Deliberate exceptions

Only these skip Azure, each a considered trade:

- **`gpt-image-1.5` on Cloudflare** for customer-facing image generation — ~10s faster, and the latency is user-visible.
- **`gpt-5.4-nano` on Cloudflare** for helloplaydate coloring pages — near-free at that size, and refuses far less on character/copyright prompts than Azure's content filter.

## Adding a provider

Store its key as a Secrets Store secret named `tiers_{provider_slug}_default` with scope `ai_gateway`, then add a step to the chain in `tiers.json`. The name *is* the binding — there is no separate provider-configuration call.

```
POST /accounts/$CF_ACCOUNT_ID/secrets_store/stores/$CF_SECRETS_STORE_ID/secrets
[{"name":"tiers_<provider>_default","value":"","scopes":["ai_gateway"]}]
```

Needs Secrets Store scope on `CLOUDFLARE_API_TOKEN`; AI-Gateway scope alone returns `10000 Authentication error`.
