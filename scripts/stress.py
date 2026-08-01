#!/usr/bin/env python3
"""Concurrency and failover stress test for the tier chains.

    python3 scripts/stress.py            # 40 requests, 10 at a time, every tier
    python3 scripts/stress.py 100 20     # 100 requests, 20 concurrent

Three things get checked, because a chain can fail in three different ways:

  1. Throughput — do the tiers hold up under concurrency, or does per-deployment
     TPM throttling start returning errors?
  2. Attribution — which provider actually answered? A tier that silently stops
     being served by Azure is working but no longer free.
  3. Failover — a chain whose first step is deliberately broken must still
     return 200 from a later step. This is the property the whole design rests
     on, and it is invisible in normal operation.

Exit code is non-zero if any request fails or failover doesn't fire.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

from aigw import TIERS, build_chain  # noqa: E402

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 40
CONCURRENCY = int(sys.argv[2]) if len(sys.argv) > 2 else 10
CHAT_TIERS = [t for t in TIERS if t != "embed"]
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def endpoint() -> str:
    return f"https://gateway.ai.cloudflare.com/v1/{os.environ['CF_ACCOUNT_ID']}/tiers"


def headers(project: str) -> dict[str, str]:
    return {
        "cf-aig-authorization": f"Bearer {os.environ['CF_AIG_TOKEN']}",
        "Content-Type": "application/json",
        "cf-aig-metadata": json.dumps({"project": project}),
    }


def fire(chain: list, project: str, timeout: float = 180.0) -> tuple[bool, float, str]:
    t0 = time.time()
    try:
        r = httpx.post(endpoint(), headers=headers(project), json=chain, timeout=timeout)
        dt = time.time() - t0
        # The universal endpoint does not set cf-aig-model (that's a dynamic-route
        # header), so which provider answered is read from the gateway log below.
        return r.status_code == 200, dt, ""
    except Exception as e:
        return False, time.time() - t0, type(e).__name__


def run(label: str, chain_for, n: int, project: str) -> tuple[int, list[float], dict[str, int]]:
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(lambda i: fire(chain_for(i), project), range(n)))
    ok = sum(1 for good, _, _ in results if good)
    lat = [dt for _, dt, _ in results]
    who: dict[str, int] = {}
    p95 = statistics.quantiles(lat, n=20)[-1] if len(lat) > 1 else lat[0]
    mark = GREEN + "ok" + OFF if ok == n else RED + "FAIL" + OFF
    print(f"  {mark}  {label:<10} {ok}/{n}  median {statistics.median(lat):5.1f}s  p95 {p95:5.1f}s")
    return ok, lat, who


def main() -> int:
    per = max(1, TOTAL // len(CHAT_TIERS))
    print(f"{TOTAL} requests, {CONCURRENCY} concurrent, {per} per tier\n")
    failures = 0
    all_who: dict[str, int] = {}

    print("throughput + attribution")
    for tier in CHAT_TIERS:
        ok, _, who = run(tier, lambda i, t=tier: build_chain(t, f"Reply with one word: pong {i}"), per, "stress")
        failures += per - ok
        for k, v in who.items():
            all_who[k] = all_who.get(k, 0) + v

    print("\nfailover — first step deliberately broken, later step must answer")
    resource = os.environ.get("AZURE_RESOURCE", "")
    broken = [
        {"provider": "azure-openai",
         "endpoint": f"{resource}/NOPE-does-not-exist/chat/completions?api-version=2024-10-21",
         "headers": {"Content-Type": "application/json"},
         "query": {"messages": [{"role": "user", "content": "hi"}]}},
        *build_chain("low", "Reply with one word: pong"),
    ]
    ok, _, who = run("broken-1st", lambda i: broken, max(4, CONCURRENCY // 2), "stress-failover")
    failed_over = ok > 0 and all("NOPE" not in m for m in who)
    failures += 0 if failed_over else 1
    print(f"    {'fell through as designed' if failed_over else RED + 'FAILOVER DID NOT FIRE' + OFF}")

    print("\nwho actually answered (from the gateway log)")
    time.sleep(5)
    counts: dict[str, int] = {}
    try:
        url = (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}"
               f"/ai-gateway/gateways/tiers/logs?per_page=50")
        rows = httpx.get(url, headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"},
                         timeout=30).json().get("result", [])
        for row in rows:
            meta = row.get("metadata") or {}
            if str(meta.get("project", "")).startswith("stress"):
                key = f"{row.get('provider')}/{row.get('model')}"
                counts[key] = counts.get(key, 0) + 1
    except Exception as e:
        print(f"    {DIM}(log read unavailable: {e}){OFF}")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        flag = "" if k.startswith("azure-openai/") else f"  {RED}<- not Azure{OFF}"
        print(f"    {v:>4}  {k}{flag}")
    if counts and any(not k.startswith("azure-openai/") for k in counts):
        print(f"    {RED}Azure did not serve everything{OFF} — throttled or down; "
              "the chain covered it, but check the resource.")

    print(f"\n  {'all good' if not failures else str(failures) + ' failures'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
