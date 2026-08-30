"""Shared helpers for the tool-calling capture hooks.

Dependency-free (stdlib only) and fast — these run inline on every Bash tool
call, so they must not import heavy libs or block. All disk writes are appends.

Two responsibilities live here:
  1. Capture      — where/how we persist observed tool calls (measurement).
  2. Drift match  — compare a command against the delta store to flag likely
                    version-drift (stale-syntax) candidates and, when hints are
                    enabled, suggest a correction.

Capture location resolves in this order:
  $TCE_CAPTURE_DIR  →  ~/.claude/tokenomics-tce/
We default OUTSIDE the repo on purpose: the hook fires for *every* Claude Code
session on this machine, not just when the cwd is this project. Point whatever
consumes the logs at the same dir.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

# --- paths -----------------------------------------------------------------

def capture_dir() -> str:
    d = os.environ.get("TCE_CAPTURE_DIR") or os.path.expanduser(
        "~/.claude/tokenomics-tce"
    )
    os.makedirs(d, exist_ok=True)
    return d


def calls_log() -> str:
    return os.path.join(capture_dir(), "tool_calls.jsonl")


def delta_store_path() -> str:
    # The store lives beside these scripts so it versions with the code, but an
    # env override lets you point at your own edited/learned copy.
    return os.environ.get("TCE_DELTA_STORE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "delta_store.json"
    )


def inject_enabled() -> bool:
    """Pre-call correction is OFF by default, so recorded runs reflect what the
    agent does unaided. Set TCE_INJECT=1 to turn the hints on."""
    return os.environ.get("TCE_INJECT", "0") not in ("0", "", "false", "False")


# --- io --------------------------------------------------------------------

def append_jsonl(path: str, obj: dict[str, Any]) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def read_stdin_json() -> dict[str, Any]:
    import sys
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def load_delta_store() -> dict[str, Any]:
    try:
        with open(delta_store_path()) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"version": 0, "deltas": []}


# --- drift detection -------------------------------------------------------

# Error-output signatures that strongly suggest STALE usage rather than a
# genuine reasoning bug — the CLI is telling us the syntax itself is wrong.
# Kept conservative: presence flags a *candidate*; a delta-store match confirms.
STALE_ERROR_SIGNATURES = [
    r"unknown (?:flag|shorthand flag|command|option)",
    r"no such (?:option|command|subcommand)",   # Click (Click-based CLIs) + git
    r"flag provided but not defined",
    r"is not a docker command",
    r"unrecognized arguments",
    r"has been (?:removed|deprecated)",
    r"deprecated",
    r"invalid choice",              # argparse-style CLIs (aws, etc.)
    r"unknown subcommand",
    r"unexpected extra argument",   # Click positional drift
    r"did you mean",
]
_STALE_RE = re.compile("|".join(STALE_ERROR_SIGNATURES), re.IGNORECASE)


def looks_stale(error_text: str | None) -> bool:
    return bool(error_text and _STALE_RE.search(error_text))


def match_deltas(command: str, store: dict[str, Any]) -> list[dict[str, Any]]:
    """Return delta entries whose match patterns fire on `command`."""
    hits: list[dict[str, Any]] = []
    for d in store.get("deltas", []):
        pats = (d.get("match") or {}).get("any_regex", [])
        for p in pats:
            try:
                if re.search(p, command):
                    hits.append(d)
                    break
            except re.error:
                continue
    return hits


def suggest_fix(command: str, delta: dict[str, Any]) -> Optional[str]:
    """Apply a delta's suggested rewrite to produce a corrected command."""
    s = delta.get("suggest")
    if not s or "regex" not in s:
        return None
    try:
        fixed = re.sub(s["regex"], s.get("replacement", ""), command)
    except re.error:
        return None
    return fixed if fixed != command else None
