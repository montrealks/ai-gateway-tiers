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

from aigw import (  # noqa: E402
    POLICIES, ROUTES, TIERS, build_chain, build_route_element, chat, embed,
)
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


def _model_of(element: dict) -> str:
    """The model an already-built chain element will call.

    Azure carries it in the endpoint path; every other provider puts it in the
    body, so read whichever is present.
    """
    q = element.get("query") or {}
    if q.get("model"):                       # anthropic / groq / cerebras
        return str(q["model"])
    ep = element["endpoint"]
    if "models/" in ep:                      # google: v1beta/models/<model>:generateContent
        return ep.split("models/", 1)[1].split(":", 1)[0]
    return ep.split("/")[1]                  # azure: <resource>/<model>/<path>?...


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
            continue  # a route, exercised below
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

    # `embed` is no longer in TIERS, so it is not in the default `wanted` list —
    # run it unless the caller named specific tiers and left it out.
    if not sys.argv[1:] or "embed" in wanted:
        try:
            v = embed("hello world", project=PROJECT)
            assert len(v) == 1536, f"expected 1536 dims, got {len(v)}"
            print(f"  {GREEN}ok{OFF}   embed      -> {len(v)} dims")
        except Exception as e:
            failures += 1
            print(f"  {RED}FAIL{OFF} embed      {str(e)[:120]}")

    # `policy` reorders a tier's chain and may NEVER change which models it can
    # use. Check both halves — the reorder happened, and the set is identical —
    # then prove an unknown name RAISES rather than being quietly ignored.
    if "high" in wanted:
        base = [s["model"] for s in TIERS["high"]["chain"]]
        got = [_model_of(e) for e in build_chain("high", "x", policy="best")]
        want_lead = TIERS["high"]["policy_order"]["best"][0]
        if got[0] != want_lead:
            failures += 1
            print(f"  {RED}FAIL{OFF} policy     best did not lead with {want_lead}: {got}")
        elif sorted(got) != sorted(base):
            failures += 1
            print(f"  {RED}FAIL{OFF} policy     best changed the model set: "
                  f"{sorted(base)} -> {sorted(got)}")
        else:
            try:
                t0 = time.time()
                out = chat("high", "Reply with exactly one word: pong",
                           project=PROJECT, policy="best")
                print(f"  {GREEN}ok{OFF}   policy     best {time.time()-t0:5.1f}s  "
                      f"{out.strip()[:20]!r}  {DIM}(reorder only, same models){OFF}")
            except Exception as e:
                failures += 1
                print(f"  {RED}FAIL{OFF} policy     best {str(e)[:110]}")

        try:
            build_chain("high", "x", policy="nonsense")
            failures += 1
            print(f"  {RED}FAIL{OFF} policy     unknown name was accepted silently")
        except KeyError:
            print(f"  {GREEN}ok{OFF}   policy     unknown name raises  {DIM}(known: "
                  f"{', '.join(POLICIES)}){OFF}")

    # `embed` is a ROUTE: one model, no chain. Prove the shape, not just the call
    # — a second model here poisons every index silently.
    try:
        el = build_route_element("embed", embed_input="x")
        assert "embed" not in TIERS, "embed is back in the tiers table"
        assert ROUTES["embed"].get("chain") is None, "the embed route grew a chain"
        assert "embeddings" in el["endpoint"], el["endpoint"]
        print(f"  {GREEN}ok{OFF}   route      embed pins "
              f"{ROUTES['embed']['model']}  {DIM}(no chain){OFF}")
    except Exception as e:
        failures += 1
        print(f"  {RED}FAIL{OFF} route      embed {str(e)[:120]}")

    # The Google tail's whole premise is that Google cannot bill us. Check it
    # rather than trust it.
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
