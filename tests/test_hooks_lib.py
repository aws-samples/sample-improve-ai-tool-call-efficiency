"""Unit tests for hook drift-matching helpers (stdlib unittest)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import lib  # noqa: E402


STORE = {
    "deltas": [
        {
            "id": "docker-compose-v1-to-v2",
            "match": {"any_regex": [r"\bdocker-compose\b"]},
            "suggest": {"regex": r"\bdocker-compose\b", "replacement": "docker compose"},
        },
        {
            "id": "docker-build-compress-removed",
            "match": {"any_regex": [r"docker build[^\n]*--compress"]},
            "suggest": {"regex": r"\s--compress\b", "replacement": ""},
        },
    ]
}


class TestLooksStale(unittest.TestCase):
    def test_unknown_flag(self):
        self.assertTrue(lib.looks_stale("Error: unknown flag: --foo"))

    def test_is_not_a_docker_command(self):
        self.assertTrue(lib.looks_stale("docker: 'compose' is not a docker command."))

    def test_deprecated(self):
        self.assertTrue(lib.looks_stale("Warning: this option has been removed"))

    def test_genuine_error_not_flagged(self):
        self.assertFalse(lib.looks_stale("fatal: pathspec 'x.py' did not match"))

    def test_empty(self):
        self.assertFalse(lib.looks_stale(""))
        self.assertFalse(lib.looks_stale(None))


class TestMatchDeltas(unittest.TestCase):
    def test_matches_docker_compose(self):
        hits = lib.match_deltas("docker-compose up -d", STORE)
        self.assertEqual([h["id"] for h in hits], ["docker-compose-v1-to-v2"])

    def test_no_false_match(self):
        self.assertEqual(lib.match_deltas("docker compose up", STORE), [])

    def test_multiple_can_match(self):
        hits = lib.match_deltas("docker build --compress .", STORE)
        self.assertIn("docker-build-compress-removed", [h["id"] for h in hits])


class TestSuggestFix(unittest.TestCase):
    def test_rewrites_hyphen_form(self):
        d = STORE["deltas"][0]
        self.assertEqual(lib.suggest_fix("docker-compose up", d), "docker compose up")

    def test_removes_flag(self):
        d = STORE["deltas"][1]
        self.assertEqual(lib.suggest_fix("docker build --compress .", d), "docker build .")

    def test_no_change_returns_none(self):
        d = STORE["deltas"][0]
        self.assertIsNone(lib.suggest_fix("docker compose up", d))


class TestInjectDefaultOff(unittest.TestCase):
    def test_off_by_default(self):
        os.environ.pop("TCE_INJECT", None)
        self.assertFalse(lib.inject_enabled())

    def test_on_when_set(self):
        os.environ["TCE_INJECT"] = "1"
        try:
            self.assertTrue(lib.inject_enabled())
        finally:
            os.environ.pop("TCE_INJECT", None)


if __name__ == "__main__":
    unittest.main()
