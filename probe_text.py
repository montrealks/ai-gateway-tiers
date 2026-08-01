#!/usr/bin/env python3
"""Latency + correctness probe over the Azure deployments, on tier-shaped work.

Vision turned out not to discriminate between candidates, so this measures the
other thing the tiers actually do: structured extraction, instruction following
and deterministic transformation. Every task has a checkable answer, so a fast
model that gets it wrong scores as wrong.

Runs each task twice per model and reports median latency, to blunt cold-start.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time

import httpx

RESOURCE = "kristiferszabo-0182-resource"
API_VERSION = "2024-10-21"

MODELS = sys.argv[1:] or [
    "gpt-5-mini", "gpt-5.6-luna",
    "gpt-5.4", "gpt-5.6-terra", "gpt-5.6-sol",
    "DeepSeek-V4-Pro", "DeepSeek-V4-Flash",
]

TASKS = [
    (
        "extract",
        'From: "Invoice 7741 dated 3 March 2026, total EUR 1,284.50, due in 30 days, '
        'vendor Kalmar Logistics AB." Return JSON exactly: '
        '{"invoice":<int>,"currency":<str>,"total":<float>,"vendor":<str>}',
        lambda o: (o.get("invoice") == 7741 and o.get("currency") == "EUR"
                   and abs(float(o.get("total", 0)) - 1284.50) < 0.01
                   and "Kalmar" in str(o.get("vendor", ""))),
    ),
    (
        "reason",
        "A train leaves at 14:20 and the journey takes 3 hours 55 minutes. It is "
        "delayed 40 minutes at departure. Return JSON exactly: "
        '{"arrival":"HH:MM"} using a 24-hour clock.',
        lambda o: str(o.get("arrival", "")).strip() == "18:55",
    ),
    (
        "instruction",
        'Return JSON exactly: {"words":[...]} containing every word in this list '
        "that has a double letter, preserving order: "
        "['balloon','cat','summer','dog','kitten','tree','apple','fox']",
        lambda o: [w.lower() for w in o.get("words", [])] ==
                  ["balloon", "summer", "kitten", "tree", "apple"],
    ),
]


def call(model: str, prompt: str) -> tuple[float, dict | None]:
    url = f"https://gateway.ai.cloudflare.com/v1/{os.environ['CF_ACCOUNT_ID']}/tiers"
    body = {"messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}}
    element = {
        "provider": "azure-openai",
        "endpoint": f"{RESOURCE}/{model}/chat/completions?api-version={API_VERSION}",
        "headers": {"Content-Type": "application/json"},
        "query": body,
    }
    t0 = time.time()
    try:
        r = httpx.post(url, headers={
            "cf-aig-authorization": f"Bearer {os.environ['CF_AIG_TOKEN']}",
            "Content-Type": "application/json",
            "cf-aig-metadata": json.dumps({"project": "text-probe"}),
        }, json=[element], timeout=300)
        dt = time.time() - t0
        if r.status_code >= 400:
            return dt, None
        txt = r.json()["choices"][0]["message"]["content"] or ""
        s, e = txt.find("{"), txt.rfind("}")
        return dt, json.loads(txt[s : e + 1]) if s != -1 and e > s else None
    except Exception:
        return time.time() - t0, None


def main() -> int:
    print(f"{'model':<20}{'correct':>9}{'median s':>10}{'total s':>9}   per-task")
    for model in MODELS:
        lat, ok, marks = [], 0, []
        for name, prompt, check in TASKS:
            best = None
            for _ in range(2):
                dt, out = call(model, prompt)
                lat.append(dt)
                good = bool(out) and bool(check(out))
                best = good if best is None else (best or good)
            ok += 1 if best else 0
            marks.append(f"{name}{'ok' if best else 'XX'}")
        print(f"  {model:<18}{ok}/{len(TASKS):<7}{statistics.median(lat):>9.1f}"
              f"{sum(lat):>9.1f}   {' '.join(marks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
