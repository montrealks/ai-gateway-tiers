#!/usr/bin/env python3
"""Prove the tier system works and that Azure is answering.

    python3 selftest.py            # every tier
    python3 selftest.py low high   # named tiers only

Checks, per tier: the call returns, and which provider actually answered. A tier
answered by anything other than Azure means Azure is throttled or down — the
chain did its job, but it is worth knowing.

Also verifies json_mode, vision, and embeddings, since those take different
wire paths per provider and are the parts most likely to break silently.

Needs CF_ACCOUNT_ID, CF_AIG_TOKEN, and CLOUDFLARE_API_TOKEN (the last only for
reading back the gateway log to see who answered).
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import httpx  # noqa: E402

from aigw import PROFILES, TIERS, build_chain, chat, embed  # noqa: E402
from verify_free_tier import main as verify_free_tier  # noqa: E402

PROJECT = "selftest"
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def _tiny_jpeg() -> str | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _who_answered(limit: int = 25) -> dict[str, str]:
    """Map model -> provider from the gateway log, for the most recent calls."""
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CF_ACCOUNT_ID")
    if not (token and account):
        return {}
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account}"
        f"/ai-gateway/gateways/tiers/logs?per_page={limit}"
    )
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        rows = r.json().get("result", [])
    except Exception:
        return {}
    return {
        str(row.get("model")): str(row.get("provider"))
        for row in rows
        if row.get("metadata", {}) and row["metadata"].get("project") == PROJECT
    }


def main() -> int:
    wanted = sys.argv[1:] or list(TIERS)
    failures = 0

    for tier in wanted:
        if tier == "embed":
            continue
        t0 = time.time()
        try:
            out = chat(tier, "Reply with exactly one word: pong", project=PROJECT)
            print(f"  {GREEN}ok{OFF}   {tier:<8} {time.time()-t0:5.1f}s  {out.strip()[:30]!r}")
        except Exception as e:
            failures += 1
            print(f"  {RED}FAIL{OFF} {tier:<8} {str(e)[:120]}")

    if "low" in wanted:
        try:
            got = chat("low", 'Return JSON {"ok":true} and nothing else.',
                       json_mode=True, project=PROJECT)
            assert isinstance(got, dict) and got.get("ok") is True, got
            print(f"  {GREEN}ok{OFF}   json_mode  -> {got}")
        except Exception as e:
            failures += 1
            print(f"  {RED}FAIL{OFF} json_mode  {str(e)[:120]}")

        img = _tiny_jpeg()
        if img is None:
            print(f"  {DIM}skip vision (Pillow not installed){OFF}")
        else:
            try:
                got = chat("low", 'Dominant colour? Reply JSON {"colour":"..."}',
                           images=[img], json_mode=True, project=PROJECT)
                print(f"  {GREEN}ok{OFF}   vision     -> {got}")
            except Exception as e:
                failures += 1
                print(f"  {RED}FAIL{OFF} vision     {str(e)[:120]}")

    if "embed" in wanted:
        try:
            v = embed("hello world", project=PROJECT)
            assert len(v) == 1536, f"expected 1536 dims, got {len(v)}"
            print(f"  {GREEN}ok{OFF}   embed      -> {len(v)} dims")
        except Exception as e:
            failures += 1
            print(f"  {RED}FAIL{OFF} embed      {str(e)[:120]}")

    # The `client` profile is what production client sites call. It inverts the
    # Azure-first order to lead with the Google free tier, so it exercises a
    # different first step than everything above and fails independently.
    if "low" in wanted and "client" in PROFILES:
        order = [s["provider"] for s in TIERS["low"]["chain"]]
        got = [
            "google-ai-studio" if "generateContent" in e["endpoint"] else e["provider"]
            for e in build_chain("low", "x", profile="client")
        ]
        if got[0] != "google-ai-studio":
            failures += 1
            print(f"  {RED}FAIL{OFF} profile    client did not lead with Google: {got}")
        elif sorted(got) != sorted(order):
            failures += 1
            print(f"  {RED}FAIL{OFF} profile    client changed the model set: "
                  f"{sorted(order)} -> {sorted(got)}")
        else:
            try:
                t0 = time.time()
                out = chat("low", "Reply with exactly one word: pong",
                           project=PROJECT, profile="client")
                print(f"  {GREEN}ok{OFF}   profile    client {time.time()-t0:5.1f}s  "
                      f"{out.strip()[:20]!r}  {DIM}(reorder only, same models){OFF}")
            except Exception as e:
                failures += 1
                print(f"  {RED}FAIL{OFF} profile    client {str(e)[:110]}")

    # The `client` profile's whole premise is that Google cannot bill us.
    # Check it rather than trust it.
    if verify_free_tier() != 0:
        failures += 1

    time.sleep(4)  # let the gateway log settle
    answered = _who_answered()
    if answered:
        print("\n  who answered:")
        for model, provider in answered.items():
            mark = "" if provider == "azure-openai" else f"  {RED}<- not Azure{OFF}"
            print(f"    {provider:<18} {model}{mark}")
        if any(p != "azure-openai" for p in answered.values()):
            print(f"\n  {RED}Azure did not answer everything{OFF} — throttled or down. "
                  "The chain covered it, but check the Azure resource.")
    else:
        print(f"\n  {DIM}(set CLOUDFLARE_API_TOKEN to see which provider answered){OFF}")

    print(f"\n  {'all good' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
