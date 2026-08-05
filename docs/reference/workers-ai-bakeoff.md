---
type: reference
title: Cloudflare Workers AI — measured bakeoff for the post-Azure chain
description: "Which neuron-billed open-weight models can actually take over a tier when the Microsoft credits expire (~2026-09-21). Verdict: llama-3.2-3b-instruct wins on every axis; the big reasoning models are the least reliable."
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

## Results (2026-08-05)

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

**Smaller beat bigger, decisively.** `llama-3.2-3b` was the most reliable, the
fastest, and the cheapest — 34x cheaper than gemma-4-26b and 15x cheaper than
gemma on latency, at 100% vs 100%. There is no reason to reach past it for
extract/classify/rewrite work.

**The reasoning models are the trap.** `gpt-oss-20b` scored **0%** and
`gpt-oss-120b` 44%. They spend their completion budget thinking and then return
`content: null` — a failure that looks like a flake until you run it three times.
`gpt-oss-120b` also fabricated freely when unconstrained (invented a venue name,
"a reusable water bottle", "a smile", and a specific date/timezone from text
containing none of them).

Same shape as the DeepSeek-V4 warning already in `tiers.json`: thinking tokens are
billed, invisible, and here they also break the contract.

**Speed labels lie.** `glm-4.7-flash` was the second-slowest model tested.

**Free headroom.** At 1.64 neurons/call and a 10,000 neurons/day allowance,
`llama-3.2-3b` gives **~6,100 free calls/day** — more than this whole estate uses.
Kris is on the **paid** Workers plan, so past the allowance it bills rather than
stops.

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

    google-ai-studio (free tier) -> workers-ai llama-3.2-3b -> anthropic (paid)

Two free steps before anything bills, and the middle step is fast enough
(693ms) to sit on a user-facing path.
