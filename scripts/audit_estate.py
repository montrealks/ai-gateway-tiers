#!/usr/bin/env python3
"""Snapshot the whole Google Cloud + Cloudflare AI Gateway estate.

Run it before a migration and after, then diff the two — the point is to prove
that capability was preserved, not merely that the commands exited zero.

    python3 scripts/audit_estate.py baseline     # -> .tmp/estate-baseline.json
    python3 scripts/audit_estate.py final        # -> .tmp/estate-final.json
    python3 scripts/audit_estate.py --diff baseline final

For each Google project it records billing state, enabled services, API keys and
their restrictions, service accounts, and 30-day request counts. For each
Cloudflare gateway it records stored provider secrets and dynamic routes, and —
this is the part that actually matters — LIVE PROBES each gateway to see whether
it can still answer.

Credentials: gcloud must be authenticated. The Cloudflare management key is read
from the login keychain (CF_CLI_KEY) so it never lives in an env var.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

ROOT = pathlib.Path(__file__).parent.parent
OUT = ROOT / ".tmp"
GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

GATEWAYS = ["tiers", "route1views", "kboodle", "helloplaydate", "profilo", "default"]


def sh(*args: str, timeout: int = 120) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def cf_key() -> str:
    return sh("security", "find-generic-password", "-s", "CF_CLI_KEY", "-w")


def gcloud_token() -> str:
    return sh("gcloud", "auth", "print-access-token")


# --- Google ------------------------------------------------------------------


def projects() -> list[str]:
    out = sh("gcloud", "projects", "list", "--format=value(projectId)")
    live = []
    for p in out.splitlines():
        # `projects list` lags behind deletions; describe gives the true state.
        if sh("gcloud", "projects", "describe", p, "--format=value(lifecycleState)") == "ACTIVE":
            live.append(p)
    return live


def usage_30d(project: str, token: str) -> dict[str, int]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    try:
        r = httpx.get(
            f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "filter": 'metric.type="serviceruntime.googleapis.com/api/request_count"',
                "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "aggregation.alignmentPeriod": "2592000s",
                "aggregation.perSeriesAligner": "ALIGN_SUM",
                "aggregation.crossSeriesReducer": "REDUCE_SUM",
                "aggregation.groupByFields": 'resource.label."service"',
            },
            timeout=60,
        )
        data = r.json()
    except Exception:
        return {}
    out: dict[str, int] = {}
    for ts in data.get("timeSeries", []):
        svc = ts["resource"]["labels"].get("service", "?")
        n = sum(int(p["value"].get("int64Value", 0)) for p in ts.get("points", []))
        if n:
            out[svc] = out.get(svc, 0) + n
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def api_keys(project: str) -> list[dict[str, Any]]:
    uids = sh("gcloud", "services", "api-keys", "list",
              f"--project={project}", "--format=value(uid)").splitlines()
    keys = []
    for uid in uids:
        raw = sh("gcloud", "services", "api-keys", "describe", uid,
                 f"--project={project}", "--format=json")
        if not raw:
            continue
        try:
            k = json.loads(raw)
        except json.JSONDecodeError:
            continue
        restr = k.get("restrictions", {})
        targets = [t.get("service") for t in restr.get("apiTargets", []) or []]
        referrers = (restr.get("browserKeyRestrictions", {}) or {}).get("allowedReferrers", [])
        ips = (restr.get("serverKeyRestrictions", {}) or {}).get("allowedIps", [])
        # Two independent axes. A key restricted to one API but usable from
        # anywhere is still usable by anyone who finds it; a key locked to a
        # domain but valid for every API is a wide blast radius on one site.
        api_restricted = bool(targets)
        app_restricted = bool(referrers or ips)
        keys.append({
            "uid": uid,
            "name": k.get("displayName", ""),
            "api_targets": targets,
            "allowed_referrers": referrers,
            "allowed_ips": ips,
            "api_restricted": api_restricted,
            "app_restricted": app_restricted,
            "unrestricted": not api_restricted and not app_restricted,
        })
    return keys


def google_snapshot(token: str) -> dict[str, Any]:
    snap = {}
    for p in projects():
        billing = sh("gcloud", "billing", "projects", "describe", p,
                     "--format=value(billingEnabled)") == "True"
        services = sorted(
            s.replace(".googleapis.com", "")
            for s in sh("gcloud", "services", "list", f"--project={p}",
                        "--format=value(config.name)").splitlines()
        )
        snap[p] = {
            "display_name": sh("gcloud", "projects", "describe", p, "--format=value(name)"),
            "billing_enabled": billing,
            "services": services,
            "api_keys": api_keys(p),
            "service_accounts": sh("gcloud", "iam", "service-accounts", "list",
                                   f"--project={p}", "--format=value(email)").splitlines(),
            "usage_30d": usage_30d(p, token),
            # The specific hazard that caused the July bill.
            "can_bill_for_llm": billing and "generativelanguage" in services,
        }
    return snap


# --- Cloudflare ---------------------------------------------------------------


def cf_snapshot(key: str, account: str) -> dict[str, Any]:
    h = {"Authorization": f"Bearer {key}"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}"

    secrets: list[str] = []
    try:
        stores = httpx.get(f"{base}/secrets_store/stores", headers=h, timeout=30).json()
        sid = stores["result"][0]["id"]
        got = httpx.get(f"{base}/secrets_store/stores/{sid}/secrets",
                        headers=h, params={"per_page": 100}, timeout=30).json()
        secrets = sorted(s["name"] for s in got.get("result", []))
    except Exception:
        pass

    gateways: dict[str, Any] = {}
    for gw in GATEWAYS:
        entry: dict[str, Any] = {
            "secrets": [s for s in secrets if s.startswith(f"{gw}_")],
            "routes": {},
        }
        try:
            r = httpx.get(f"{base}/ai-gateway/gateways/{gw}/routes", headers=h, timeout=30).json()
            for route in r.get("data", {}).get("routes", []):
                detail = httpx.get(
                    f"{base}/ai-gateway/gateways/{gw}/routes/{route['id']}",
                    headers=h, timeout=30,
                ).json()
                steps = []
                for el in detail.get("result", {}).get("version", {}).get("data", []):
                    if el.get("type") == "model":
                        p = el.get("properties", {})
                        steps.append({
                            "provider": p.get("provider"),
                            "model": p.get("model"),
                            "retries": p.get("retries"),
                        })
                entry["routes"][route["name"]] = steps
        except Exception:
            pass
        gateways[gw] = entry
    return {"all_secrets": secrets, "gateways": gateways}


# --- live probes --------------------------------------------------------------


def probe(account: str, token: str) -> dict[str, Any]:
    """Can each path actually answer right now? This is the capability test."""
    results: dict[str, Any] = {}
    body = {"contents": [{"parts": [{"text": "reply with the single word: pong"}]}]}

    for gw in ("kboodle", "route1views"):
        url = (f"https://gateway.ai.cloudflare.com/v1/{account}/{gw}"
               f"/google-ai-studio/v1beta/models/gemini-3.1-flash-lite:generateContent")
        results[f"{gw}:gemini-direct"] = _try(url, token, body)

    results["route1views:dynamic-low"] = _try(
        f"https://gateway.ai.cloudflare.com/v1/{account}/route1views/compat/chat/completions",
        token,
        {"model": "dynamic/low", "messages": [{"role": "user", "content": "say pong"}]},
    )
    return results


def _try(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        r = httpx.post(
            url,
            headers={
                "cf-aig-authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                # Cloudflare's edge 403s scripted clients with no User-Agent.
                "User-Agent": "curl/8.7.1",
            },
            json=payload,
            timeout=60,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    ok = r.status_code == 200
    return {"ok": ok, "status": r.status_code,
            "detail": "" if ok else r.text[:160]}


# --- report / diff ------------------------------------------------------------


def report(snap: dict[str, Any]) -> None:
    print("\n  google projects")
    for p, d in sorted(snap["google"].items()):
        flag = f"  {RED}<- CAN BILL FOR LLM{OFF}" if d["can_bill_for_llm"] else ""
        bill = f"{RED}billed{OFF}" if d["billing_enabled"] else f"{GREEN}free{OFF}"
        total = sum(d["usage_30d"].values())
        print(f"    {p:<32} {bill:<18} {total:>7,} req/30d{flag}")
        for k in d["api_keys"]:
            if k["unrestricted"]:
                print(f"        {RED}unrestricted key{OFF}: {k['name']}  (any API, any caller)")
            elif not k.get("app_restricted"):
                print(f"        {YELLOW}no app restriction{OFF}: {k['name']}  "
                      f"({len(k['api_targets'])} API(s), usable by anyone holding it)")

    print("\n  cloudflare gateways")
    for gw, d in snap["cloudflare"]["gateways"].items():
        routes = ", ".join(
            f"{n}[{' -> '.join(s['provider'] or '?' for s in steps)}]"
            for n, steps in d["routes"].items()
        ) or "no routes"
        print(f"    {gw:<16} {routes}")
        if d["secrets"]:
            print(f"        {DIM}{', '.join(s.split('_', 1)[1] for s in d['secrets'])}{OFF}")

    print("\n  live probes")
    for name, r in snap["probes"].items():
        mark = f"{GREEN}ok{OFF}" if r["ok"] else f"{RED}FAIL{OFF}"
        print(f"    {mark:<14} {name:<32} {r.get('detail','')[:70]}")


def diff(a: dict[str, Any], b: dict[str, Any]) -> int:
    """Compare two snapshots. Non-zero if capability regressed."""
    regressions = 0
    print("\n  capability (probes)")
    for name in sorted(set(a["probes"]) | set(b["probes"])):
        was = a["probes"].get(name, {}).get("ok")
        now = b["probes"].get(name, {}).get("ok")
        if was and not now:
            regressions += 1
            print(f"    {RED}REGRESSED{OFF}  {name}")
        elif was != now:
            print(f"    {GREEN}improved {OFF}  {name}  {was} -> {now}")
        else:
            print(f"    {DIM}unchanged{OFF}  {name}  ({now})")

    print("\n  billing exposure")
    # Only compare projects present in BOTH snapshots — a project that did not
    # exist has no prior value, and `None != False` would otherwise read as a
    # new risk when the new project is in fact clean.
    for p in sorted(set(a["google"]) & set(b["google"])):
        wa = bool(a["google"][p].get("can_bill_for_llm"))
        nb = bool(b["google"][p].get("can_bill_for_llm"))
        if wa != nb:
            arrow = f"{GREEN}fixed{OFF}" if wa and not nb else f"{RED}NEW RISK{OFF}"
            print(f"    {arrow}  {p}")
    for p in sorted(set(b["google"]) - set(a["google"])):
        if b["google"][p].get("can_bill_for_llm"):
            print(f"    {RED}NEW RISK{OFF}  {p} (new project, can bill for LLM)")
    gone = sorted(set(a["google"]) - set(b["google"]))
    new = sorted(set(b["google"]) - set(a["google"]))
    if gone:
        print(f"    {DIM}projects removed:{OFF} {', '.join(gone)}")
    if new:
        print(f"    {DIM}projects added:{OFF} {', '.join(new)}")

    print(f"\n  {'no capability regressions' if not regressions else str(regressions) + ' REGRESSION(S)'}")
    return regressions


def main() -> int:
    OUT.mkdir(exist_ok=True)
    args = sys.argv[1:]

    if args and args[0] == "--diff":
        a = json.loads((OUT / f"estate-{args[1]}.json").read_text())
        b = json.loads((OUT / f"estate-{args[2]}.json").read_text())
        return 1 if diff(a, b) else 0

    label = args[0] if args else "snapshot"
    key, token = cf_key(), gcloud_token()
    if not key or not token:
        print(f"  {RED}need gcloud auth and the CF_CLI_KEY keychain entry{OFF}")
        return 1

    import os
    account = os.environ["CF_ACCOUNT_ID"]
    aig = os.environ["CF_AIG_TOKEN"]

    snap = {
        "label": label,
        "google": google_snapshot(token),
        "cloudflare": cf_snapshot(key, account),
        "probes": probe(account, aig),
    }
    path = OUT / f"estate-{label}.json"
    path.write_text(json.dumps(snap, indent=1))
    report(snap)
    print(f"\n  written to {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
