"""Offline regression tests; never build settings or call the Claude CLI."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import selfcheck


class JudgeTests(unittest.TestCase):
    def test_missing_file_is_not_silently_ignored(self):
        ok, failed = selfcheck.judge(
            [{"type": "file_exists", "path": "report.txt"}], {}, "Done")
        self.assertFalse(ok)
        self.assertEqual(len(failed), 1)

    def test_required_tool_is_not_silently_ignored(self):
        ok, _ = selfcheck.judge(
            [{"type": "tool_used", "tool": "Write"}], {}, "Done")
        self.assertFalse(ok)

    def test_turn_limit_without_tools_or_files(self):
        ok, _ = selfcheck.judge(
            [{"type": "max_turns", "n": 4}], {"turns": 5}, "Done")
        self.assertFalse(ok)

    def test_unknown_assertion_fails(self):
        ok, _ = selfcheck.judge([{"type": "typo"}], {}, "Done")
        self.assertFalse(ok)

    def test_python_check_executes_real_assertion(self):
        assertion = {"type": "python_check", "path": "answer.py",
                     "code": "import answer; assert answer.value == 42"}
        for value, expected in [(42, True), (41, False)]:
            with self.subTest(value=value):
                ok, _ = selfcheck.judge(
                    [assertion], {"files": {"answer.py": f"value = {value}\n"}}, "")
                self.assertEqual(ok, expected)


class CoverageTests(unittest.TestCase):
    def run_suite(self, probes, assertions=None):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "cases").mkdir()
            (root / "cases" / "cases.json").write_text(json.dumps({"cases": [
                {"id": "sample", "assertions": assertions if assertions is not None
                 else [{"type": "output_matches", "re": "^yes$"}]}]}))
            (root / "cases" / "probes.json").write_text(json.dumps(probes))
            with mock.patch.object(selfcheck, "ROOT", root), \
                    contextlib.redirect_stdout(io.StringIO()):
                return selfcheck.main()

    def test_missing_case_fails(self):
        self.assertEqual(self.run_suite({}), 1)

    def test_empty_probe_list_fails(self):
        self.assertEqual(self.run_suite({"sample": []}), 1)

    def test_missing_positive_or_negative_fails(self):
        for expect, text in [("pass", "yes"), ("fail", "no")]:
            with self.subTest(expect=expect):
                self.assertEqual(self.run_suite({"sample": [
                    {"expect": expect, "text": text}]}), 1)

    def test_invalid_expect_fails(self):
        self.assertEqual(self.run_suite({"sample": [
            {"expect": "pass", "text": "yes"},
            {"expect": "fail", "text": "no"},
            {"expect": "typo", "text": "no"}]}), 1)

    def test_empty_assertions_fail(self):
        self.assertEqual(self.run_suite({"sample": [
            {"expect": "pass", "text": "yes"},
            {"expect": "fail", "text": "no"}]}, assertions=[]), 1)

    def test_unknown_case_fails(self):
        self.assertEqual(self.run_suite({"unknown": [
            {"expect": "pass", "text": "yes"}]}), 1)

    def test_complete_suite_passes(self):
        self.assertEqual(self.run_suite({"sample": [
            {"expect": "pass", "text": "yes"},
            {"expect": "fail", "text": "no"}]}), 0)


if __name__ == "__main__":
    unittest.main()
