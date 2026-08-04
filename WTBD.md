# Work To Be Done

## Goal

Replace Anthropic as the universal paid tail where a cheaper provider does the job, and fix the
place that is actually spending money. Kris's premise — that Anthropic is badly priced for
low/mid-tier work against DeepSeek/Qwen/Groq — is directionally right, but the first pass of
evidence says the urgency is not where it looks.

**What the logs actually say (2026-08-04, pre-existing chains):**

| Gateway | Anthropic calls | Model | Est. cost | Read |
|---|---|---|---|---|
| route1views | 10 / 50 logs | claude-haiku-4-5 | **$0.0096** | tail fires, but ~$0.001/call — trivial |
| helloplaydate | 9 / 50 logs | **claude-sonnet-5** | **$0.7613** | 27–32k token prompts. NOT a tail — deliberate high-tier work |
| profilo | 5 / 17 logs | — | $0.0002 | trivial |
| kboodle | 0 | — | $0 | never fires |

So the fallback tail costs roughly a cent per three days, while one *deliberate* Sonnet workload
on helloplaydate costs ~80x everything the tails spend combined. The tail is the wrong target
today; helloplaydate's Sonnet usage is the right one.

**Two things change that calculus:**
1. route1views' tail numbers predate today's Azure insertion, which should cut tail firing
   sharply. Needs re-measuring before drawing conclusions.
2. **Azure credits expire ~2026-09-21.** After that the chain becomes Gemini free → Azure PAID →
   Anthropic PAID, and the tail's price stops being academic. tiers.json already names
   DeepSeek-V4-Flash (~$0.14/$0.28 per 1M) and gpt-5.6-luna (~$0.20/$1.20) as the candidates.

**Already verified reachable:** `groq` answers 200 through the gateway on a free-tier key that is
already stored (`tiers_groq_default`). Groq is free and very fast — a strong tail candidate that
costs nothing to trial. `workers-ai` returned 400 and `cerebras` 404 on first attempt; both are
probably wrong endpoint/model ids rather than genuine unavailability, so they need one more look.

### Constraints

- **route1views is the oldest client and does not tolerate disruption.** Any task touching its
  production path is FLAG FIRST: research and propose, do not execute unattended.
- **Do not swap the tail on evidence-free preference.** Headline per-token price is not the whole
  story: DeepSeek V4 runs thinking mode by default and bills invisible reasoning tokens at the
  output rate (tiers.json `_measured`), which both narrows the real gap and hurts latency. The
  creator-helper path is user-facing, so latency counts.
- Serial execution. These tasks mutate shared live infrastructure and this session's standing
  instruction is not to dispatch subagents.
- Pause and report on anything that confounds the plan.

## Execution Protocol (read every iteration)
1. Every iteration tackles exactly ONE task.
2. Read the Progress notes left by the previous iteration before starting.
3. Full research for the task at hand. Before starting, identify the back-pressure — the test,
   command or observable signal that will prove the task afterward.
4. Do the work. Apply config changes everywhere they appear; never patch one site.
5. Run the smoketest after the task, plus the back-pressure from step 3.
6. Commit (repo changes only). Never mention Claude / Claude Code / AI in the commit message.
7. Mark the task complete here (check its box).
8. Leave a dated note under Progress.
9. One task at a time. Do not stop until every task is checked off, except at a FLAG FIRST task.

**Smoketest:** `python3 scripts/selftest.py`
**Harness:** `python3 scripts/audit_estate.py <label>` / `--diff <a> <b>`

## Tasks

- [ ] A. Re-measure the tail now that Azure sits in every chain — 30-day Anthropic spend and fire
      rate per gateway. Establishes whether the tail is a problem at all.
- [x] B. Investigate helloplaydate's `claude-sonnet-5` workload — **ANSWERED by an existing audit**;
      see Progress. Supersedes several tasks below.
- [ ] C. Fix the endpoint/model ids for `workers-ai` and `cerebras`, then benchmark viable tail
      candidates against the REAL workload (spam-score JSON reliability + latency, not "say pong").
      **Constraint discovered: Azure DeepSeek-V4 has tool calling DISABLED and is text-only**, so it
      cannot serve tool-using or vision work — it is only a candidate for plain text tails.
- [~] J. **IN PROGRESS (6/7 — all RUNTIME sites complete)** — port helloplaydate's call sites to named tiers
      (`face_check.py`, `character_bible.py` ×2, `likeness_gate.py`, `vision_meta.py`,
      `storybook/narrative.py`, `premade_search.py`). Volume-scaled premium spend; separate repo.
- [ ] K. Add prompt caching to the analytics-digest agent (~$8/mo -> ~$4/mo per the audit).
- [ ] D. Decide the tail on that evidence and write the decision + the post-2026-09-21 chain into
      tiers.json, including what to re-evaluate when credits lapse.
- [ ] E. Swap the tail on the low-stakes gateways first (profilo, then helloplaydate, then
      kboodle) and measure for a day.
- [ ] F. **FLAG FIRST** — swap route1views' tail once E has shown a clean day.
- [ ] G. Restrict `earnest-vine-120619` "API key 1" — currently unrestricted (any API, any
      caller); it is the last fully-open key in the estate.
- [ ] H. Evaluate folding earnest-vine into route1views *(recommendation: DEFER — see note)*
- [ ] I. Re-run the harness, diff against `final`, confirm no capability regressions.

## Progress
<!-- newest note first; one entry per completed task -->

### 2026-08-04 — POST-REFACTOR AUDIT (production, not code-reading)
Tested the live systems rather than inspecting code. Five findings, two of them serious.

**1. kboodle production is on BOB'S Cloudflare account — my kboodle work had ZERO effect.**
Production `wp-config.php` sets `KBOODLE_CF_AI_GATEWAY_ACCOUNT_ID = 1e10432c85f1e8d9866094cfd24f1777`,
which is `Bobrohinsky@gmail.com's Account`, not Kris's. Everything I did to the `kboodle` gateway
(BYOK google secret, azure secret, dynamic route) was applied to the gateway of the SAME NAME in
Kris's account, which production never calls. Worse: Bob's gateway has NO google-ai-studio secret
at all — only `fal` — so its Gemini calls run on **Cloudflare unified billing** against Bob's
account (`cost=2e-06`, `cached=False`, real token counts). kboodle production has never been on a
free tier. Production DOES work (verified live: returns "pong").

**2. route1views production does NOT use the dynamic route, so task M does not protect it.**
Production wp-config: `R1V_LLM_PROVIDER=google-ai-studio`, `R1V_LLM_MODEL=gemini-3.1-flash-lite` —
NOT `compat` / `dynamic/low` as the LOCAL wp-config has. Production calls Gemini directly with NO
fallback. The Azure step I added to the `low` route is real but unreached. Production DOES work
(verified through its own LlmService via wp-cli: 1.66s, correct answer) and my key swap did not
break it, since it uses `route1views_google-ai-studio_default`, which I repointed.

**3. Lyria (music generation) is now DEAD estate-wide — a real capability loss.**
`lyria-3-clip-preview` returns `429 RESOURCE_EXHAUSTED` on the free key: Lyria has no free-tier
entitlement. And no billed project can serve it any more (swept: zero billed projects have
generativelanguage enabled). Combination of the billed project being deleted and my machine-wide
GEMINI_API_KEY swap. Affects `~/.claude/skills/media-use/audio/scripts/lyria-recipe.py`,
`lib/bgm.mjs`, media-factory and video-gen BGM. Arguably the correct failure — it fails instead of
silently billing — but it IS a lost capability and needs a decision.

**4. The VPS is unreachable over Tailscale** (`dial tcp 100.96.57.39:22: i/o timeout`, last seen
~3h). helloplaydate.com serves 200 so the app is fine, but deploys and the aigw diagnostic
workflow are blocked. NOT caused by this refactor, but it blocks verification of the container env.

**5. Clean:** cnxlocal.com live (200) with no build/runtime coupling to places-scout, so deleting
that project was safe. places-scout worker verified end-to-end. The google secret I added to the
helloplaydate gateway is INERT — no app code calls google-ai-studio through that gateway (all such
log entries were my own probes), so no rate-limit regression there.

### 2026-08-04 — Task J: all runtime sites done (6 of 7)
Every user-facing hardcoded model id in helloplaydate now names a TIER. Each verified with a LIVE
call, against the behaviour that matters rather than a smoke test:

| Site | Tier | Live verification |
|---|---|---|
| `face_check` | low | real verdict 4.6s; served by azure-openai/gpt-5.6-luna |
| `premade_search.break_story` | low | 4 correctly-personalised scenes, 5.4s |
| `character_bible` (coarse) | low | — |
| `character_bible` (bible) | **high** | deep-brown/black-haired subject read back "Black", "medium-deep brown skin" — no lightening |
| `likeness_gate` | low | whitewash still HARD-FAILS on both protected attributes, drift named |
| `storybook/narrative` | low | 12 spreads for age 4, kid-safe, warm title |

`character_bible`'s bible describer took `high`, not `low`, deliberately: it drives the child's
real colouring into every generation, and `high` still ends at claude-sonnet-5 so the model it
used to name outright stays reachable as the last fallback.

**Behavioural fix found while porting:** `likeness_gate` caught only `httpx.HTTPError` per sample.
The tier client raises `TierError`, which would have escaped and turned an unavailable backend
into a raised exception mid-promotion — breaking the module's documented fail-OPEN contract.
Widened to `Exception`.

Only hardcoded `claude-` ids left under `app/`: `settings.analytics_digest_model` (correct — the
audit says KEEP; tool-calling agent, and Azure's DeepSeek has tool calling disabled) and
`vision_meta.MODEL`.

**Remaining: `vision_meta` — needs a decision, not a swap.** OFFLINE ingest tooling, no runtime
callers, but two scripts import its API by name: `scripts/_vision_sweep.py` and
`scripts/vision_pass.py` both do `from app.services.vision_meta import MODEL, ...`, and
`vision_pass` also imports `usage_cost_usd`. The tier client does not expose token usage, so
porting makes `_usage` zeros and `usage_cost_usd` meaningless — arguably CORRECT once the call is
free on Azure credits, but it silently changes what those scripts report. Three files plus a
reporting-semantics change; flagged rather than assumed.

### 2026-08-04 — Task J in progress (2 of 7 sites)
Working in an isolated git worktree at `~/Projects/.wt-hpd-llm-port`, branch
`chore/port-hardcoded-llm-callsites`, based on helloplaydate HEAD. Kris has uncommitted WIP on
`feat/seasonal-marketing-layer` (templates + copy_contract) and untracked docs, so landing
LLM-cost commits there would tangle unrelated history. His main tree is untouched.

**Done and verified LIVE (not just mocked):**
1. `face_check.py` — the highest-volume site (every photo upload, 4 routers). Now `chat("low",
   images=[...], json_mode=True)`. Live vision call returned a real verdict in 4.6s and the
   gateway log confirms `azure-openai / gpt-5.6-luna`, project `helloplaydate/face_check` — i.e.
   the FREE first step. Net -70 lines.
2. `premade_search.break_story` — text-only, async, now `achat("low", json_mode=True)`. Live call
   produced 4 correctly-personalised scenes in 5.4s.

**Gotchas worth carrying to the remaining sites:**
- The tier client has NO `system` parameter (a system prompt is not portable across a chain), so
  fold the framing into the prompt.
- `json_mode` maps to `response_format: json_object` on the Azure steps, which REJECTS a top-level
  array. Ask for `{"scenes": [...]}` and unwrap.
- These sites all used the helloplaydate gateway; the tier client uses the `tiers` gateway. Pass
  `project="helloplaydate/<site>"` so per-site spend stays attributable.
- `tests/test_face_check.py` was ALREADY FAILING on master — it cleared the gateway settings to
  exercise a direct-provider path a later change forbade. Rewritten against the tier client;
  11/11 pass and it no longer needs credentials at all.

**Remaining, in value order:** `character_bible.py` (2 sites, one is **claude-sonnet-5** on a
per-child multi-sample path — highest remaining value), `likeness_gate.py`,
`storybook/narrative.py`, then `vision_meta.py` last: it is an OFFLINE ingest tool with retry
logic and Haiku-specific cost tracking (`usage_cost_usd`, `PRICE_*_PER_MTOK`) that needs rework
rather than a swap, and it has no runtime callers.

### 2026-08-04 — Task B ANSWERED (by a pre-existing audit, not by me)
`~/Projects/helloplaydate/docs/audits/2026-08-03-digest-model-cost-audit.md` already investigated
exactly this, one day before, prompted by the same instinct Kris raised here.

**What the tokens are:** the analytics-digest agent — a native Anthropic Messages-API `tool_use`
loop with 8 tool definitions, `max_turns=16`, 600s budget, and a terminal `send_digest_email`
tool. It runs **once per day**. The 27-32k prompts are the agent loop accumulating conversation
across turns, not one huge prompt. ~$8/mo.

**Why it cannot move to DeepSeek** — the audit's central finding, and it directly constrains this
plan: **Azure's DeepSeek-V4 deployments have tool calling DISABLED**, and V4 is **text-only**. So
DeepSeek cannot serve tool-using OR vision workloads at all. That kills the naive "swap Anthropic
for DeepSeek" move for anything but plain text.

**Where the real money is** (audit §5): six *hardcoded* Claude call sites doing volume-scaled work,
while `outline_reviewer.py` and `planner/ai.py` next door correctly name a tier:
- `face_check.py` — claude-haiku on **every photo upload**, from 4 routers. Highest-volume premium call.
- `character_bible.py` — **claude-sonnet-5** vision, run `BIBLE_SAMPLES` times per child.
- plus `likeness_gate.py`, `vision_meta.py`, `storybook/narrative.py`, `premade_search.py`.

Correct target is the `low` tier (Azure gpt-5.6-luna -> gpt-5.4, both vision-capable, free on MS
credits). `premade_search.py` is text-only and could go to `offload` today.

**Consequence for this plan:** the digest stays on Sonnet (verdict upheld), and the tail swap drops
in priority again. New tasks J and K carry the actual value. J is in a different repo.

### 2026-08-04 — Plan created (research notes)

**On earnest-vine (task H) — my recommendation is to defer the merge.** It hosts the Google Photos
Picker OAuth client (`619124313045-…`, i.e. its own project number) plus the API key that
route1views' wp-config reads as `R1V_GOOGLE_PHOTOS_API_KEY`. Folding it into route1views means
minting a new OAuth client, and **OAuth grants are per-client-id** — every user who has already
authorised Google Photos would be forced to re-consent. That is user-visible friction on a live
feature belonging to the client least tolerant of disruption, and the payoff is one fewer *free*
project. The genuinely valuable half is task G, restricting that unrestricted key, which can be
done in place without moving anything.

**Anti-goal for this plan:** swapping the tail because Anthropic is unfashionable. The tails cost
about a cent per three days. If B shows helloplaydate's Sonnet workload can move to a cheaper
model, that single change is worth more than every tail swap combined — so B outranks E and F even
though it appears later in the dependency order.
