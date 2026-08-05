#!/usr/bin/env python3
"""Bake off Cloudflare Workers AI text models on the work this estate actually does.

Written to answer one question, later: when the Azure credits expire (~2026-09-21),
is any neuron-billed open-weight model good enough to take over a tier?

    python3 scripts/bakeoff_workers_ai.py            # full run
    python3 scripts/bakeoff_workers_ai.py --quick    # 1 rep instead of 3

Three tasks, chosen because they mirror real call sites rather than a vibe check:

  extract   strict JSON from messy event text, and it must NOT invent fields.
            Mirrors kboodle's event services. Scored on schema validity AND
            fabrication — a model that confidently adds a plausible venue is
            worse than one that returns null.
  classify  spam scoring as strict JSON. Mirrors route1views SpamScoreService,
            which fails open, so an unparseable reply silently lets spam through.
  rewrite   short user-facing copy. Mirrors the AI-help/rewrite paths. Scored on
            latency and whether every input fact survived.

Reliability is the headline number, not quality. Several of these models are
REASONING models that spend their completion budget thinking and then return
empty content — which looks like a flake until you run it three times.

Costs are read from the `cf-ai-neurons` response header, not estimated.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from typing import Any

import httpx

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

MODELS = [
    "@cf/meta/llama-3.2-3b-instruct",
    "@cf/meta/llama-3.1-8b-instruct-fp8",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/meta/llama-4-scout-17b-16e-instruct",
    "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/google/gemma-4-26b-a4b-it",
    "@cf/qwen/qwen3-30b-a3b-fp8",
    "@cf/zai-org/glm-4.7-flash",
    "@cf/openai/gpt-oss-20b",
    "@cf/openai/gpt-oss-120b",
]

EVENT_TEXT = (
    "park cleanup saturday 9am at the pavilion, bring gloves and rakes if you "
    "have them, coffee provided, all ages welcome, rain or shine"
)

# Facts genuinely present in EVENT_TEXT. Anything a model adds beyond these is
# fabrication, which is the failure mode that matters for event extraction.
EXPECTED_BRING = {"gloves", "rakes"}

TASKS: dict[str, dict[str, Any]] = {
    "extract": {
        "prompt": (
            "Extract ONLY facts stated in this text. Do not invent or embellish. "
            f'If a field is not stated, use null. Text: "{EVENT_TEXT}"'
        ),
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": ["string", "null"]},
                "time": {"type": ["string", "null"]},
                "location": {"type": ["string", "null"]},
                "bring": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "time", "location", "bring"],
        },
    },
    "classify": {
        "prompt": (
            "Score this user submission for spam. Reply with JSON only.\n"
            'Submission: "Great view!! Check out cheap-watches-deals dot com for 90% off '
            'rolex replicas, limited time, click now!!!"'
        ),
        "schema": {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "is_spam": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["score", "is_spam", "reason"],
        },
    },
    "rewrite": {
        "prompt": (
            "Rewrite this community event listing to be clearer and more inviting. "
            "Under 55 words. Keep EVERY fact, add none. Text: " + EVENT_TEXT
        ),
        "schema": None,
    },
}


def cf_key() -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", "CF_CLI_KEY", "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip()


def call(account: str, key: str, model: str, task: dict) -> dict:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": task["prompt"]}]}
    if task["schema"]:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "out", "schema": task["schema"]},
        }
    t0 = time.time()
    try:
        r = httpx.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body, timeout=120,
        )
    except Exception as e:
        return {"ok": False, "ms": 0, "neurons": 0.0, "text": "", "err": str(e)[:60]}
    ms = int((time.time() - t0) * 1000)
    neurons = float(r.headers.get("cf-ai-neurons") or 0)
    try:
        d = r.json()
    except Exception:
        return {"ok": False, "ms": ms, "neurons": neurons, "text": "", "err": "non-json"}
    if not d.get("success", True):
        return {"ok": False, "ms": ms, "neurons": neurons, "text": "",
                "err": str(d.get("errors"))[:60]}
    res = d.get("result", {}) or {}
    text = res.get("response")
    if text is None:
        choices = res.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content")
    return {"ok": True, "ms": ms, "neurons": neurons, "text": text or "", "err": ""}


def parse_json(text: Any) -> Any:
    """Tolerant parse — a model that fences its JSON is sloppy, not broken.

    Some models honour `response_format` by returning an already-decoded object
    rather than a JSON string, so accept both shapes.
    """
    if isinstance(text, (dict, list)):
        return text
    if not text:
        return None
    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?", "", t).rsplit("```", 1)[0].strip()
    try:
        return json.loads(t)
    except Exception:
        s, e = t.find("{"), t.rfind("}")
        if s != -1 and e > s:
            try:
                return json.loads(t[s : e + 1])
            except Exception:
                return None
    return None


def score(task_name: str, obj: Any, text: str) -> tuple[bool, str]:
    """Did the model do the job? Returns (passed, note)."""
    if task_name == "rewrite":
        text = text if isinstance(text, str) else json.dumps(text or "")
        low = (text or "").lower()
        missing = [w for w in ("pavilion", "glove", "coffee") if w not in low]
        if not text:
            return False, "empty"
        return (not missing), ("ok" if not missing else "dropped:" + ",".join(missing))

    if obj is None or not isinstance(obj, dict):
        return False, "no parseable JSON"

    if task_name == "extract":
        missing = [k for k in ("title", "time", "location", "bring") if k not in obj]
        if missing:
            return False, "missing keys"
        bring = {str(b).lower() for b in (obj.get("bring") or [])}
        # Fabrication check: anything beyond gloves/rakes was invented.
        extra = {b for b in bring if not any(e in b for e in EXPECTED_BRING)}
        if extra:
            return False, f"invented {sorted(extra)[:2]}"
        return True, "faithful"

    if task_name == "classify":
        if not all(k in obj for k in ("score", "is_spam", "reason")):
            return False, "missing keys"
        if obj.get("is_spam") is not True:
            return False, "missed obvious spam"
        return True, "ok"
    return False, "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--models", nargs="*", default=MODELS)
    args = ap.parse_args()
    reps = 1 if args.quick else 3

    import os
    account = os.environ["CF_ACCOUNT_ID"]
    key = cf_key()
    if not key:
        print(f"{RED}no CF_CLI_KEY in keychain{OFF}")
        return 1

    results: dict[str, dict] = {}
    for model in args.models:
        short = model.replace("@cf/", "")
        print(f"\n{short}")
        per_model = {}
        for tname, task in TASKS.items():
            passes, lats, neus, note = 0, [], [], ""
            for _ in range(reps):
                r = call(account, key, model, task)
                lats.append(r["ms"])
                neus.append(r["neurons"])
                if r["ok"]:
                    ok, n = score(tname, parse_json(r["text"]) if task["schema"] else None, r["text"])
                    passes += ok
                    note = n
                else:
                    note = r["err"] or "call failed"
            rate = passes / reps
            colour = GREEN if rate == 1 else (YELLOW if rate > 0 else RED)
            print(f"  {tname:<9} {colour}{passes}/{reps}{OFF}  "
                  f"{statistics.median(lats):>6.0f}ms  {statistics.median(neus):>6.2f}n  {DIM}{note}{OFF}")
            per_model[tname] = {
                "pass_rate": rate,
                "median_ms": statistics.median(lats),
                "median_neurons": statistics.median(neus),
                "note": note,
            }
        results[model] = per_model

    # --- verdict table -------------------------------------------------------
    print(f"\n{'model':<44} {'reliab':<8} {'med ms':<8} {'neurons':<9} verdict")
    ranked = []
    for m, per in results.items():
        rel = sum(t["pass_rate"] for t in per.values()) / len(per)
        ms = statistics.median([t["median_ms"] for t in per.values()])
        nu = sum(t["median_neurons"] for t in per.values()) / len(per)
        ranked.append((rel, -ms, m, ms, nu))
    for rel, _, m, ms, nu in sorted(ranked, reverse=True):
        v = "USABLE" if rel == 1 else ("flaky" if rel >= 0.6 else "NOT usable")
        c = GREEN if rel == 1 else (YELLOW if rel >= 0.6 else RED)
        print(f"  {m.replace('@cf/',''):<42} {c}{rel*100:>5.0f}%{OFF}  {ms:>6.0f}   {nu:>6.2f}    {c}{v}{OFF}")

    out = f"{__file__.rsplit('/',2)[0]}/.tmp/bakeoff-workers-ai.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    print(f"\n  raw -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
