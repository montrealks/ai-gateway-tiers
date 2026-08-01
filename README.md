# ai-gateway-tiers

**Ask for a capability tier, never a model.** Every app names `low`, `high`, `offload`, `code` or `embed`; this repo owns what those resolve to.

Azure leads every tier — Microsoft-for-Startups credits are free until ~2026-09-21, and there is more credit than can be spent before then. Each tier falls back to a *second Azure model* before it will consider anything that costs money.

```
tiers.json         source of truth — the chains, the policy, the stored keys
client/aigw.py     the client every app installs
```

## Use it

```bash
pip install -e ~/Projects/ai-gateway-tiers/client
```

```python
from aigw import chat, embed          # async: achat, aembed

text = chat("low", "Classify this as spam or not: ...")
cfg  = chat("low", prompt, json_mode=True)          # -> parsed dict
out  = chat("high", prompt, images=[jpeg_b64])      # vision
vec  = embed("a sentence")                          # 1536 dims
```

Needs `CF_ACCOUNT_ID` and `CF_AIG_TOKEN`. Nothing else — every provider key is stored in the gateway, so calls go keyless.

Pass `project="<app>"` on every call. It becomes `cf-aig-metadata` and is what makes spend traceable per app in the gateway logs.

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

## How it works

The client POSTs an ordered **array** of attempts to the gateway's universal endpoint at `…/v1/{account}/tiers`; the gateway returns the first that succeeds.

```json
[ {"provider":"azure-openai",
   "endpoint":"kristiferszabo-0182-resource/gpt-5.6-luna/chat/completions?api-version=2024-10-21",
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

**A Google AI Studio key is free only while its Cloud project has no billing account.** Attaching billing — even for an unrelated API like Maps — silently moves the key to the paid tier, where free quota does not apply and no 429 is emitted. The key in use lives in `gen-lang-client-0291098513`, which has no billing account and therefore cannot be charged:

```bash
gcloud billing projects describe gen-lang-client-0291098513   # billingEnabled must be false
```

**The gateway's `cost` field cannot prove anything is free.** It's a list-price estimate that ignores hidden reasoning tokens and can understate real spend by ~8x.

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
POST /accounts/$CF_ACCOUNT_ID/secrets_store/stores/d190007683ca446bb02f5aa82a3d343e/secrets
[{"name":"tiers_<provider>_default","value":"","scopes":["ai_gateway"]}]
```

Needs Secrets Store scope on `CLOUDFLARE_API_TOKEN`; AI-Gateway scope alone returns `10000 Authentication error`.
