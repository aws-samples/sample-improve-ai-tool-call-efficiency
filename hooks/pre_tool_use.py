#!/usr/bin/env python3
"""PreToolUse hook — surface a correction before the command runs. OFF by default.

Runs BEFORE a Bash command executes. In record-only mode (default) it just logs
what was about to run. With TCE_INJECT=1 it checks the command against the delta
store and, on a match, surfaces a correction hint to the agent.

Leaving injection off is useful when you want to measure your own drift rate
first, without the hints changing the behaviour you're measuring.

Hint delivery: we use the non-blocking channel — print the hint and exit 0, so
the agent sees the suggestion as context but is never hard-blocked. A stricter
variant could exit 2 to block the call outright; the soft form is the default so
a false-positive match can never stop legitimate work.

Channel note (this hook API drifted too): Claude Code's PreToolUse output schema
changed. The old `{"decision":"approve","reason":...}` form is a SILENT NO-OP on
modern CC (>=2.x) — the reason text never reaches the model, so the hint would go
nowhere. The current contract delivers extra context to the model via
`hookSpecificOutput.additionalContext`. We emit BOTH: the modern key (verified on
CC 2.1.212 to reach the model) plus the legacy keys for older CC.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import (  # noqa: E402
    append_jsonl,
    capture_dir,
    inject_enabled,
    load_delta_store,
    match_deltas,
    read_stdin_json,
    suggest_fix,
)


def main() -> int:
    payload = read_stdin_json()
    tool = payload.get("tool_name") or payload.get("tool") or "?"
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    sid = payload.get("session_id") or payload.get("sessionId")

    # Always log the intent (pre-image), so we can pair pre/post per call.
    append_jsonl(
        os.path.join(capture_dir(), "pre_tool_use.jsonl"),
        {"session_id": sid, "tool": tool, "command": command, "inject": inject_enabled()},
    )

    if tool != "Bash" or not command or not inject_enabled():
        return 0

    hits = match_deltas(command, load_delta_store())
    if not hits:
        return 0

    lines = ["⚠️ this command may use STALE CLI syntax:"]
    for d in hits:
        note = d.get("note", "")
        fixed = suggest_fix(command, d)
        lines.append(f"  • [{d.get('id')}] {note}")
        if fixed:
            lines.append(f"    suggested: {fixed}")

    # Record that a hint was surfaced, so its effect can be attributed later.
    append_jsonl(
        os.path.join(capture_dir(), "interventions.jsonl"),
        {"session_id": sid, "command": command, "deltas": [d.get("id") for d in hits]},
    )

    # Soft hint via structured JSON stdout. Emit the MODERN schema
    # (hookSpecificOutput.additionalContext — the only channel that actually
    # reaches the model on CC >=2.x) AND the legacy keys for older CC. stderr is
    # kept as a human-visible breadcrumb; it is not the model-delivery path.
    hint = "\n".join(lines)
    print(hint, file=sys.stderr)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": hint,
            "additionalContext": hint,
        },
        # legacy keys (pre-2.x CC) — harmless on modern versions:
        "decision": "approve",
        "reason": hint,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # never break the session
