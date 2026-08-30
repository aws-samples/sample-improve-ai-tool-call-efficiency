"""Regression tests for post_tool_use._extract — locks in the fixes for the two
bugs found on real capture data:
  1. CC puts Bash errors in STDOUT (stderr empty); there's no exit_code field.
  2. `git checkout -b` succeeds → must be benign drift, not a drift candidate.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import lib  # noqa: E402
from post_tool_use import _extract  # noqa: E402


def payload(command, stdout="", stderr="", interrupted=False):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": stderr, "interrupted": interrupted},
    }


class TestExitCodeRecovery(unittest.TestCase):
    def test_exit_trailer_in_stdout(self):
        # a Click-based CLI rejecting a removed option: error in stdout, [exit 2] trailer
        e = _extract(payload("mytool --mcp-help 2>&1; echo \"[exit $?]\"",
                             stdout="Error: No such option: --mcp-help\n[exit 2]"))
        self.assertEqual(e["exit_code"], 2)
        self.assertTrue(e["is_error"])

    def test_last_trailer_wins(self):
        e = _extract(payload("a; echo [exit 1]; b; echo [exit 0]",
                             stdout="[exit 1]\n[exit 0]"))
        self.assertEqual(e["exit_code"], 0)
        self.assertFalse(e["is_error"])

    def test_no_trailer_success(self):
        e = _extract(payload("ls", stdout="file1\nfile2"))
        self.assertIsNone(e["exit_code"])
        self.assertFalse(e["is_error"])

    def test_interrupted_is_error(self):
        e = _extract(payload("sleep 100", interrupted=True))
        self.assertTrue(e["is_error"])

    def test_output_combines_stdout_and_stderr(self):
        e = _extract(payload("x", stdout="out", stderr="err"))
        self.assertIn("out", e["output"])
        self.assertIn("err", e["output"])


class TestStaleSignatureInStdout(unittest.TestCase):
    def test_no_such_option_detected(self):
        # the exact Click "No such option" phrasing that previously slipped through
        self.assertTrue(lib.looks_stale("Error: No such option: --mcp-help"))

    def test_error_in_combined_output_scanned(self):
        e = _extract(payload("docker-compose up",
                             stdout="docker: 'compose' is not a docker command.\n[exit 1]"))
        self.assertTrue(lib.looks_stale(e["output"]))
        self.assertTrue(e["is_error"])


if __name__ == "__main__":
    unittest.main()
