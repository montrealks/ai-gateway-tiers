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
- [ ] J. **Highest value** — port helloplaydate's six hardcoded Claude call sites to named tiers
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
