#!/usr/bin/env python3
"""Concurrent load test for a tiered AI Gateway.

Usage:  python3 scripts/stress-test.py [total] [concurrency]

Reads CF_ACCOUNT_ID / CF_AIG_TOKEN from the environment, or from a .env file at
the repo root (see .env.example). Cost-bounded by design: the request mix is
weighted heavily toward the cheap tier and every reply is capped at 8 tokens.

Captures HTTP status, latency, and the cf-aig-model response header — which is
how you observe failover from the free primary to the paid fallback under load.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter

# --- env: real environment wins, .env fills the gaps -------------------------
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

try:
    ACCT = os.environ["CF_ACCOUNT_ID"]
    TOKEN = os.environ["CF_AIG_TOKEN"]
except KeyError as e:
    sys.exit(f"missing env var {e} — copy .env.example to .env")

GATEWAY = os.environ.get("CF_AIG_GATEWAY", "tiers")
URL = f"https://gateway.ai.cloudflare.com/v1/{ACCT}/{GATEWAY}/compat/chat/completions"

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 400
CONC = int(sys.argv[2]) if len(sys.argv) > 2 else 30

# Request mix: mostly the cheap tier, a slice of the expensive one.
TIERS = ["low", "high"]
WEIGHTS = [0.92, 0.08]

mix = []
for tier, w in zip(TIERS, WEIGHTS):
    mix += [tier] * int(TOTAL * w)
mix += [TIERS[0]] * (TOTAL - len(mix))


def one(_i, tier):
    body = json.dumps({
        "model": f"dynamic/{tier}",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "reply with only: ok"}],
    }).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "cf-aig-authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "cf-aig-metadata": '{"project":"stress-test"}',
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            r.read()
            return dict(tier=tier, status=r.status, ms=(time.time() - t0) * 1000,
                        model=r.headers.get("cf-aig-model"), err=None)
    except urllib.error.HTTPError as e:
        return dict(tier=tier, status=e.code, ms=(time.time() - t0) * 1000,
                    model=e.headers.get("cf-aig-model"),
                    err=e.read()[:120].decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return dict(tier=tier, status="ERR", ms=(time.time() - t0) * 1000,
                    model=None, err=str(e)[:120])


def pct(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))]


print(f"-> {TOTAL} requests @ concurrency {CONC} against gateway '{GATEWAY}'\n")
start = time.time()
with ThreadPoolExecutor(max_workers=CONC) as ex:
    res = list(ex.map(lambda a: one(*a), enumerate(mix)))
wall = time.time() - start

ok = [r for r in res if r["status"] == 200]
lat = [r["ms"] for r in ok]
print(f"wall: {wall:.1f}s   throughput: {TOTAL/wall:.1f} req/s   "
      f"success: {len(ok)}/{TOTAL} ({100*len(ok)/TOTAL:.1f}%)")
print("status codes:", dict(Counter(r["status"] for r in res)))
if lat:
    print(f"latency ms — p50 {pct(lat,50):.0f} | p90 {pct(lat,90):.0f} | p95 {pct(lat,95):.0f} "
          f"| p99 {pct(lat,99):.0f} | max {max(lat):.0f} | mean {sum(lat)/len(lat):.0f}")

print("\nper-tier (count | ok | p50ms | p95ms | model distribution -> shows failover):")
for tier in TIERS:
    tr = [r for r in res if r["tier"] == tier]
    tok = [r for r in tr if r["status"] == 200]
    tl = [r["ms"] for r in tok]
    md = dict(Counter((r["model"] or "-") for r in tok))
    print(f"  {tier:5} | {len(tr):4} | {len(tok):4} | {pct(tl,50):5.0f} | {pct(tl,95):5.0f} | {md}")

errs = [r for r in res if r["status"] != 200]
if errs:
    print("\nerror samples (first 5):")
    for r in errs[:5]:
        print(f"  [{r['status']}] {r['tier']}: {r['err']}")
