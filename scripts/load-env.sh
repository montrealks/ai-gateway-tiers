#!/usr/bin/env bash
# Shared env loader. Sourced by the other scripts.
# Loads .env from the repo root if present; already-exported vars win.
_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
if [ -f "$_repo_root/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$_repo_root/.env"
  set +a
fi
