#!/usr/bin/env python3
"""Install the CLI version-drift hooks into your Claude Code settings.

Merges the PreToolUse (Bash) + PostToolUse (*) hook entries into
~/.claude/settings.json (or --settings PATH), pointing at this checkout's hooks/
with absolute paths. Idempotent: re-running updates the paths rather than
duplicating entries. Prints the resulting hooks block.

Usage:
    python3 install.py                 # install into ~/.claude/settings.json
    python3 install.py --inject        # also turn correction hints ON (TCE_INJECT=1)
    python3 install.py --settings ./project/.claude/settings.json
    python3 install.py --print         # just print the block, change nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "hooks")
PRE = os.path.join(HOOKS, "pre_tool_use.py")
POST = os.path.join(HOOKS, "post_tool_use.py")

MARKER = "sample-improve-ai-tool-call-efficiency"   # tag our entries so we can find + replace them


def hooks_block() -> dict:
    return {
        "PreToolUse": [{"matcher": "Bash", "_source": MARKER, "hooks": [
            {"type": "command", "command": f"python3 {PRE}"}]}],
        "PostToolUse": [{"matcher": "*", "_source": MARKER, "hooks": [
            {"type": "command", "command": f"python3 {POST}"}]}],
    }


def _strip_ours(entries: list) -> list:
    """Drop any previously-installed entries (idempotency)."""
    return [e for e in entries if not (isinstance(e, dict) and e.get("_source") == MARKER)]


def merge(settings: dict) -> dict:
    hooks = settings.setdefault("hooks", {})
    block = hooks_block()
    for event, entries in block.items():
        existing = _strip_ours(hooks.get(event, []) or [])
        hooks[event] = existing + entries
    return settings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings",
                    default=os.path.expanduser("~/.claude/settings.json"))
    ap.add_argument("--inject", action="store_true",
                    help="set TCE_INJECT=1 in settings.env — turns correction hints ON")
    ap.add_argument("--print", dest="dry", action="store_true",
                    help="print the hooks block and exit without writing")
    a = ap.parse_args()

    for p in (PRE, POST):
        if not os.path.exists(p):
            sys.exit(f"hook not found: {p} — run from the checkout root")

    if a.dry:
        print(json.dumps({"hooks": hooks_block()}, indent=2))
        return 0

    settings = {}
    if os.path.exists(a.settings):
        try:
            settings = json.load(open(a.settings))
        except json.JSONDecodeError:
            sys.exit(f"{a.settings} is not valid JSON — fix or move it first")

    merge(settings)
    if a.inject:
        settings.setdefault("env", {})["TCE_INJECT"] = "1"

    os.makedirs(os.path.dirname(a.settings), exist_ok=True)
    json.dump(settings, open(a.settings, "w"), indent=2)
    print(f"✓ installed version-drift hooks into {a.settings}")
    print(f"  PreToolUse  (Bash) → {PRE}")
    print(f"  PostToolUse (*)    → {POST}")
    print(f"  hints: {'ON (TCE_INJECT=1)' if a.inject else 'OFF — record only'}")
    if not a.inject:
        print("\n  Correction hints are OFF by default — the hooks only record.")
        print("  Re-run with --inject to enable them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
