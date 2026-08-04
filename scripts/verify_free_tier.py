#!/usr/bin/env python3
"""Assert the Google project behind every stored AI Studio key cannot be billed.

The `client` profile leads with Google on the premise that its key is free. That
premise is not a property of the key — it is a property of the key's Google Cloud
project, and it flips the instant a billing account is attached to that project
for ANY reason, including an unrelated API like Maps or Places. Google emits no
429 and no warning when it flips; the first signal is an invoice. That is exactly
how $74.30 was spent in July 2026.

So the claim is checked rather than documented:

    python3 scripts/verify_free_tier.py

Exits non-zero if the project named in tiers.json has billing enabled, or if the
check cannot be completed. Requires an authenticated `gcloud`.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

_SPEC = json.loads((pathlib.Path(__file__).parent.parent / "tiers.json").read_text())


def billing_enabled(project_id: str) -> bool | None:
    """True/False, or None if the answer could not be established."""
    if not shutil.which("gcloud"):
        return None
    try:
        out = subprocess.run(
            ["gcloud", "billing", "projects", "describe", project_id,
             "--format=value(billingEnabled)"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    answer = out.stdout.strip().lower()
    if answer in ("true", "false"):
        return answer == "true"
    return None


def main() -> int:
    spec = _SPEC.get("google_free_tier", {})
    project = spec.get("project_id")

    if not project:
        print(f"  {RED}FAIL{OFF} tiers.json google_free_tier.project_id is not set")
        return 1

    state = billing_enabled(project)

    if state is None:
        # Unverifiable is not the same as unsafe, but it must not read as a pass.
        print(f"  {YELLOW}SKIP{OFF} could not verify {project} "
              f"— gcloud missing, unauthenticated, or no permission")
        print(f"  {DIM}     run: gcloud auth login && "
              f"gcloud billing projects describe {project}{OFF}")
        return 0

    if state:
        print(f"  {RED}FAIL{OFF} {project} HAS BILLING ENABLED — "
              f"every google-ai-studio call is charged at vendor list price.")
        print(f"  {DIM}     The `client` profile assumes this project is free. "
              f"Either detach billing, or move the stored key to an unbilled "
              f"project and update tiers.json.{OFF}")
        return 1

    print(f"  {GREEN}ok{OFF}   free tier  -> {project} billingEnabled=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
