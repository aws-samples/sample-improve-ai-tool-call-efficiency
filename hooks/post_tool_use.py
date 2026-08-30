#!/usr/bin/env python3
"""PostToolUse hook — the MEASUREMENT instrument.

Runs AFTER a tool call completes. It does three things, all read-only w.r.t. the
agent (it never blocks or alters a result — exit 0 always):

  1. Record every tool call: tool, args, exit status, stderr signature, timing.
     This is the ground-truth outcome everything downstream consumes.
  2. Flag drift candidates: a Bash command that failed with a stale-usage error
     signature (unknown flag, deprecated, "is not a docker command", ...).
  3. Self-populate the delta store: if a command FAILED and a near-identical
     command SUCCEEDED shortly after in the same session, that (stale → correct)
     pair is a free labeled drift example — we log it as a candidate for review.

Claude Code passes the hook a JSON payload on stdin. Field names have shifted
across Claude Code versions — the same drift problem, one layer up — so we read
defensively.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import (  # noqa: E402
    append_jsonl,
    calls_log,
    capture_dir,
    load_delta_store,
    looks_stale,
    match_deltas,
    read_stdin_json,
)


def _extract(payload: dict) -> dict:
    """Pull the fields we care about, tolerant of schema drift across CC versions.

    Ground truth (CC PostToolUse, verified): tool_response for Bash is
    {stdout, stderr, interrupted, isImage, noOutputExpected} with NO exit_code
    and NO is_error field — and command errors land in STDOUT, not stderr. So we
    must scan combined output and recover the exit code heuristically."""
    tool = payload.get("tool_name") or payload.get("tool") or "?"
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    resp = payload.get("tool_response") or payload.get("tool_result") or {}

    # tool_response may be a dict, a string, or a list of blocks.
    interrupted = False
    explicit_error = False
    explicit_code = None
    if isinstance(resp, dict):
        stdout = resp.get("stdout") or ""
        stderr = resp.get("stderr") or ""
        interrupted = bool(resp.get("interrupted"))
        explicit_error = bool(resp.get("is_error") or resp.get("error"))
        explicit_code = resp.get("exit_code")
        if explicit_code is None:
            explicit_code = resp.get("exitCode")
        # Some CC versions fold everything into "content".
        content = resp.get("content")
        if not (stdout or stderr) and content:
            stdout = content if isinstance(content, str) else str(content)
    elif isinstance(resp, str):
        stdout, stderr = resp, ""
    else:
        stdout, stderr = str(resp), ""

    combined = f"{stdout}\n{stderr}".strip()

    # Recover the real exit code. CC doesn't give one, but agents commonly append
    # a `[exit N]` trailer; that N is the code of the command just before it.
    exit_code = explicit_code
    if exit_code is None:
        m = re.findall(r"\[exit (\d+)\]", combined)
        if m:
            exit_code = int(m[-1])  # last trailer wins for chained commands
    if exit_code is None and (interrupted or explicit_error):
        exit_code = 1

    is_error = bool(explicit_error or interrupted or (exit_code not in (0, None)))

    return {
        "tool": tool,
        "command": tool_input.get("command", "") if isinstance(tool_input, dict) else "",
        "args": tool_input if isinstance(tool_input, dict) else {"_raw": tool_input},
        "stdout_len": len(stdout),
        "output": combined[:2000],   # combined stdout+stderr, for signature scans
        "exit_code": exit_code,
        "is_error": is_error,
        "interrupted": interrupted,
    }


def main() -> int:
    payload = read_stdin_json()
    e = _extract(payload)

    # Scan COMBINED stdout+stderr for a stale-usage signature — CC puts command
    # errors in stdout, and agents often redirect stderr with `2>&1`, so scanning
    # stderr alone (the old bug) misses almost everything.
    stale_signature = e["tool"] == "Bash" and looks_stale(e["output"])

    # Delta-store match is drift *present* in the command text, regardless of
    # outcome. Split it: a match on a FAILED command is waste-inducing; a match
    # on a command that SUCCEEDED is benign drift (e.g. `git checkout -b` still
    # works). This is the benign vs waste-inducing split .
    matched = []
    if e["command"]:
        matched = [d.get("id") for d in match_deltas(e["command"], load_delta_store())]

    # A drift candidate requires an actual FAILURE plus a drift signal (either a
    # stale error signature, or a delta-store match on a failing command). A
    # successful command is never waste, no matter what it matches.
    waste_inducing = e["is_error"] and bool(stale_signature or matched)
    benign_drift = (not e["is_error"]) and bool(matched)

    record = {
        "hook": "post_tool_use",
        "session_id": payload.get("session_id") or payload.get("sessionId"),
        "cwd": payload.get("cwd"),
        "ts": payload.get("timestamp"),  # CC-supplied; may be None
        "tool": e["tool"],
        "command": e["command"],
        "args": e["args"],
        "exit_code": e["exit_code"],
        "is_error": e["is_error"],
        "interrupted": e["interrupted"],
        "output": e["output"],
        "stdout_len": e["stdout_len"],
        # drift signals:
        "stale_error_signature": stale_signature,
        "delta_store_matches": matched,
        "class8_candidate": waste_inducing,     # failure + drift → real waste
        "benign_drift": benign_drift,           # drift present, but succeeded
    }
    append_jsonl(calls_log(), record)

    # Self-populated harvest: when a command fails, keep it in a small per-session
    # "pending failures" file so the NEXT successful similar command can be paired
    # with it into a (stale → correct) example. Cheap, append-only.
    if e["tool"] == "Bash" and e["command"]:
        _harvest(record)

    return 0


def _harvest(record: dict) -> None:
    """Pair a just-succeeded command with a recent failed one → drift example."""
    import json
    import re

    sid = record.get("session_id") or "unknown"
    pending = os.path.join(capture_dir(), f"pending_fail_{sid}.jsonl")
    cmd = record["command"]

    def _base(c: str) -> str:
        # crude command fingerprint: program + first subcommand. Normalize
        # hyphens to spaces FIRST so `docker-compose` and `docker compose`
        # share a fingerprint — the hyphen->subcommand split is itself a common
        # drift pattern we must pair across.
        toks = re.findall(r"[A-Za-z0-9_.]+", c.replace("-", " "))
        return " ".join(toks[:2]).lower()

    if record["is_error"]:
        append_jsonl(pending, {"command": cmd, "base": _base(cmd)})
        return

    # success: look for a pending failure with the same fingerprint
    if not os.path.exists(pending):
        return
    try:
        with open(pending) as f:
            fails = [json.loads(l) for l in f if l.strip()]
    except (OSError, json.JSONDecodeError):
        return
    b = _base(cmd)
    for fail in fails:
        if fail.get("base") == b and fail.get("command") != cmd:
            append_jsonl(
                os.path.join(capture_dir(), "drift_examples.jsonl"),
                {
                    "session_id": sid,
                    "stale_command": fail["command"],
                    "working_command": cmd,
                    "fingerprint": b,
                    "status": "unreviewed",
                },
            )
            break


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A capture hook must NEVER break the user's session.
        sys.exit(0)
