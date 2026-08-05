---
type: reference
title: Cloudflare Workers AI — measured bakeoff for the post-Azure chain
description: "Which neuron-billed open-weight models can actually take over a tier when the Microsoft credits expire (~2026-09-21). Verdict on the REAL task: llama-4-scout-17b matches production gemini-3.1-flash-lite; llama-3.2-3b looks best on toy prompts and fails on real ones."
tags: [llm, workers-ai, neurons, cost, tiers, bakeoff]
timestamp: 2026-08-05T00:00:00Z
status: active
---

# Workers AI bakeoff

## Why this exists

The Azure credits are free until **~2026-09-21**. After that every tier's first two
steps start costing money, and the question becomes: is any neuron-billed
open-weight model good enough to take over?

This is the measured answer, so the decision does not have to be made from vendor
claims in the week the credits lapse.

Reproduce with `python3 scripts/bakeoff_workers_ai.py` (add `--quick` for 1 rep).

## Setup

Three tasks mirroring real call sites, 3 reps each, on Kris's own account:

| task | mirrors | pass condition |
|---|---|---|
| `extract` | kboodle event services | strict JSON **and no invented facts** |
| `classify` | route1views `SpamScoreService` | strict JSON, catches obvious spam |
| `rewrite` | AI-help / rewrite paths | keeps every input fact |

Neurons read from the `cf-ai-neurons` response header, not estimated.

## The toy-task trap (read this first)

The first run of this bakeoff used a 50-word event blurb and a 3-field extraction.
It ranked `llama-3.2-3b` the outright winner. **That result was worthless**, and
Kris caught it: the real call sites send a full post plus a 65-term taxonomy and
demand 1-3 choices drawn only from that list.

Adding the REAL route1views categoriser reversed the ranking. On the real task
`llama-3.2-3b` scores **1/3** — it invents categories that are not in the
taxonomy ("Route 1", "Food", "Travel") and ignores the 1-3 limit, returning up
to NINE. Neither failure is visible on a toy prompt.

**Never rank a model on a prompt smaller than the real one.**

## Results — REAL task (2026-08-05)

The `categorize` task is lifted verbatim from
`CreatorController::getCategorySuggestions`: same wording, same live 65-term
taxonomy, same JSON contract. Pass = valid JSON, 1-3 entries, every entry drawn
from the taxonomy.

| model | categorize | median | neurons | verdict |
|---|---|---|---|---|
| **gemini-3.1-flash-lite** *(production yardstick, paid)* | **5/5** | 1188ms | — | the bar |
| **meta/llama-4-scout-17b-16e-instruct** | **3/3** | **1053ms** | 13.4 | **matches production** |
| mistralai/mistral-small-3.1-24b-instruct | 3/3 | 1187ms | 16.8 | equal, slightly pricier |
| meta/llama-3.3-70b-instruct-fp8-fast | 3/3 | 2056ms | 22.5 | fine, 2x slower |
| qwen/qwen3-30b-a3b-fp8 | 3/3 | 5242ms | 15.0 | too slow for a UI path |
| meta/llama-3.2-3b-instruct | **1/3** | 1011ms | 3.3 | **invents categories** |

Sensible agreement between the good models: everything that passed chose
"Diners & Dives" plus "New Jersey" and/or "Route 1 Before the Interstates" —
the same picks production Gemini makes.

### Recommendation

`@cf/meta/llama-4-scout-17b-16e-instruct` is the neuron model to reach for. It
matched the production model on both reliability and latency, at ~13.4 neurons
for a categorise call — roughly **745 free categorise calls/day** inside the
10k allowance, and this account is on the paid Workers plan so it bills past
that rather than failing.

## Results — toy tasks (kept for contrast)

| model | reliability | median | neurons | verdict |
|---|---|---|---|---|
| **meta/llama-3.2-3b-instruct** | **100%** | **693ms** | **1.64** | **USABLE — winner** |
| meta/llama-4-scout-17b-16e-instruct | 100% | 1461ms | 5.15 | usable |
| meta/llama-3.3-70b-instruct-fp8-fast | 100% | 1641ms | 12.46 | usable |
| qwen/qwen3-30b-a3b-fp8 | 100% | 5202ms | 15.68 | usable, slow |
| zai-org/glm-4.7-flash | 100% | 9389ms | 29.12 | usable, slow despite the name |
| google/gemma-4-26b-a4b-it | 100% | 10688ms | 56.28 | usable, slowest + priciest |
| mistralai/mistral-small-3.1-24b-instruct | 89% | 1462ms | 4.17 | flaky — dropped JSON 1/3 |
| openai/gpt-oss-120b | 44% | 3344ms | 19.64 | **NOT usable** |
| meta/llama-3.1-8b-instruct-fp8 | 33% | 440ms | 0.84 | **NOT usable** (fastest, but wrong) |
| openai/gpt-oss-20b | 0% | 2030ms | 8.45 | **NOT usable** |

## What to take from it

**Size only stops mattering once the task is trivial.** On toy prompts the 3B
model topped the table; on the real 65-category task it was the only model that
failed. The toy result was measuring formatting, not capability. Where a real
constraint exists — a closed vocabulary, a count limit — capacity starts to tell,
and `llama-4-scout-17b` is the smallest model here that holds it.

**The reasoning models are the trap.** `gpt-oss-20b` scored **0%** and
`gpt-oss-120b` 44%. They spend their completion budget thinking and then return
`content: null` — a failure that looks like a flake until you run it three times.
`gpt-oss-120b` also fabricated freely when unconstrained (invented a venue name,
"a reusable water bottle", "a smile", and a specific date/timezone from text
containing none of them).

Same shape as the DeepSeek-V4 warning already in `tiers.json`: thinking tokens are
billed, invisible, and here they also break the contract.

**Speed labels lie.** `glm-4.7-flash` was the second-slowest model tested.

**Free headroom.** At ~13.4 neurons for a real categorise call against a 10,000
neurons/day allowance, `llama-4-scout-17b` gives roughly **745 free calls/day** —
still comfortably above this estate's usage. Kris is on the **paid** Workers plan,
so past the allowance it bills rather than stops. (The 1.64 neurons/call figure
for `llama-3.2-3b` only applies to the toy prompts it passed.)

## Caveats

- Tool calling was NOT part of the scored bakeoff. Separately verified:
  `gpt-oss-120b` **does** support OpenAI-shaped `tool_calls` correctly. That is
  the one thing it is good for, and it matters because Azure's DeepSeek-V4 has
  tool calling DISABLED — see the helloplaydate digest audit.
- Vision was not tested. Only `llama-3.2-11b-vision-instruct` in this catalogue
  handles images, so the `low` tier's vision work cannot move here wholesale.
- Embeddings are a separate story: 0.01 neurons (~1M/day free) but **no 1536-dim
  model**, so adopting them means re-embedding every stored vector.

## If a tier moves here

Chain order for a post-credits `low` would be:

    google-ai-studio (free tier) -> workers-ai llama-4-scout-17b -> anthropic (paid)

Two free steps before anything bills, and the middle step is fast enough
(1053ms — faster than the production model it would be standing in for) to sit
on a user-facing path.
