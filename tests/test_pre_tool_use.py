"""Tests for pre_tool_use.main — the correction path.

Covers the two things that would silently stop corrections from landing:
  1. The hint must ride on `hookSpecificOutput.additionalContext` (the only
     channel that reaches the model on CC >=2.x; legacy decision/reason is a
     silent no-op there).
  2. Injection must stay OFF unless TCE_INJECT is set, so record-only runs stay
     record-only — but the pre-image must be logged either way.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import pre_tool_use  # noqa: E402


def payload(command, tool="Bash", sid="s1"):
    return {"session_id": sid, "tool_name": tool, "tool_input": {"command": command}}


class PreHookCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tce_pre_")

    def run_hook(self, obj, inject=False, raw=None):
        """Drive main() with `obj` on stdin; return (rc, parsed_stdout_or_None)."""
        env = {"TCE_CAPTURE_DIR": self.dir, "TCE_INJECT": "1" if inject else "0"}
        text = raw if raw is not None else json.dumps(obj)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdin", io.StringIO(text)), \
                redirect_stdout(out), redirect_stderr(err):
            rc = pre_tool_use.main()
        raw_out = out.getvalue().strip()
        return rc, (json.loads(raw_out) if raw_out else None)

    def expect_hint(self, obj):
        """Run with hints on and assert one was emitted; return it."""
        rc, out = self.run_hook(obj, inject=True)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(out, "expected a hint on stdout, got nothing")
        return out or {}

    def read_log(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]


class TestRecordOnlyMode(PreHookCase):
    def test_no_injection_when_disabled(self):
        rc, out = self.run_hook(payload("docker-compose up"), inject=False)
        self.assertEqual(rc, 0)
        self.assertIsNone(out)
        self.assertEqual(self.read_log("interventions.jsonl"), [])

    def test_pre_image_logged_even_when_disabled(self):
        self.run_hook(payload("docker-compose up"), inject=False)
        rows = self.read_log("pre_tool_use.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["command"], "docker-compose up")
        self.assertFalse(rows[0]["inject"])


class TestCorrectionMode(PreHookCase):
    def test_hint_uses_modern_additional_context_channel(self):
        out = self.expect_hint(payload("docker-compose up -d"))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "allow")
        self.assertIn("docker compose", hso["additionalContext"])

    def test_hint_names_the_delta_and_suggests_the_fix(self):
        out = self.expect_hint(payload("docker-compose up -d"))
        hint = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("docker-compose-v1-to-v2", hint)
        self.assertIn("suggested: docker compose up -d", hint)

    def test_legacy_keys_still_emitted_for_older_cc(self):
        out = self.expect_hint(payload("docker-compose up"))
        self.assertEqual(out["decision"], "approve")
        self.assertEqual(out["reason"], out["hookSpecificOutput"]["additionalContext"])

    def test_hint_is_recorded_for_later_attribution(self):
        self.run_hook(payload("docker-compose up"), inject=True)
        rows = self.read_log("interventions.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "s1")
        self.assertIn("docker-compose-v1-to-v2", rows[0]["deltas"])

    def test_clean_command_gets_no_hint(self):
        rc, out = self.run_hook(payload("ls -la"), inject=True)
        self.assertEqual(rc, 0)
        self.assertIsNone(out)
        self.assertEqual(self.read_log("interventions.jsonl"), [])

    def test_non_bash_tool_is_not_matched(self):
        # PostToolUse matches *, but only Bash carries CLI drift.
        rc, out = self.run_hook(payload("docker-compose up", tool="Read"), inject=True)
        self.assertEqual(rc, 0)
        self.assertIsNone(out)


class TestResilience(PreHookCase):
    def test_malformed_stdin_exits_clean(self):
        rc, out = self.run_hook(None, inject=True, raw="not json at all")
        self.assertEqual(rc, 0)
        self.assertIsNone(out)

    def test_empty_stdin_exits_clean(self):
        rc, out = self.run_hook(None, inject=True, raw="")
        self.assertEqual(rc, 0)
        self.assertIsNone(out)

    def test_missing_tool_input_exits_clean(self):
        rc, out = self.run_hook({"session_id": "s1", "tool_name": "Bash"}, inject=True)
        self.assertEqual(rc, 0)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
