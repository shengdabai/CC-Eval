"""Offline runner regressions. All model invocations and settings reads are mocked."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import run_eval
import selfcheck


CASE = {"id": "sample", "prompt": "answer", "split": "train", "tags": [],
        "assertions": [{"type": "output_matches", "re": "yes"}]}


class RunnerTests(unittest.TestCase):
    def run_case(self, event=None, *, stdout=None, error=None, budget=1):
        event = event if event is not None else {
            "type": "result", "subtype": "success", "result": "yes",
            "modelUsage": {"claude-test": {}}, "total_cost_usd": 0.1}
        proc = subprocess.CompletedProcess([], 0, stdout if stdout is not None
                                           else json.dumps(event), "")
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(run_eval, "prepare_cwd", return_value=pathlib.Path(td)), \
                mock.patch.object(run_eval.subprocess, "run", return_value=proc,
                                  side_effect=error) as cli:
            row = run_eval.run_case(CASE, "opus", "high", pathlib.Path("unused"),
                                    [], 1, budget, 0)
            return row, cli

    def test_budget_never_rounded_up(self):
        row, cli = self.run_case(budget=0.019)
        self.assertEqual(row["status"], "ok")
        cmd = cli.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.01")

    def test_subcent_or_invalid_budget_never_calls_cli(self):
        for amount in (0, -1, 0.009, float("nan"), float("inf")):
            with self.subTest(amount=amount):
                row, cli = self.run_case(budget=amount)
                self.assertEqual(row["status"], "error")
                cli.assert_not_called()

    def test_timeout_and_missing_result_have_unknown_cost(self):
        for kwargs in ({"error": subprocess.TimeoutExpired("mock", 1)}, {"stdout": ""}):
            with self.subTest(kwargs=kwargs):
                row, _ = self.run_case(**kwargs)
                self.assertEqual(row["status"], "error")
                self.assertTrue(row.get("cost_unknown"))

    def test_missing_binary_is_error_instead_of_crash(self):
        row, _ = self.run_case(error=FileNotFoundError("mock executable"))
        self.assertEqual(row["status"], "error")

    def test_setup_failure_does_not_call_cli(self):
        with mock.patch.object(run_eval, "prepare_cwd", side_effect=OSError("fixture")), \
                mock.patch.object(run_eval.subprocess, "run") as cli:
            row = run_eval.run_case(CASE, "opus", "high", pathlib.Path("unused"), [], 1, 1, 0)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["cost"], 0)
        cli.assert_not_called()

    def test_nonobject_stream_records_are_ignored(self):
        valid = json.dumps({"type": "result", "result": "yes",
                            "modelUsage": {"claude-test": {}}, "total_cost_usd": 0.1})
        row, _ = self.run_case(stdout='[]\nnull\n"hello"\n' + valid)
        self.assertEqual(row["status"], "ok")

    def test_malformed_assistant_blocks_do_not_crash_runner(self):
        valid = json.dumps({"type": "result", "result": "yes",
                            "modelUsage": {"claude-test": {}}, "total_cost_usd": 0.1})
        records = [{"type": "assistant", "message": None},
                   {"type": "assistant", "message": {"content": [None, "text", {}]}}]
        row, _ = self.run_case(stdout="\n".join(map(json.dumps, records)) + "\n" + valid)
        self.assertEqual(row["status"], "ok")

    def test_invalid_cost_or_identity_is_error(self):
        for field, values in (("total_cost_usd", [-1, "0.2", None, float("nan")]),
                              ("modelUsage", [None, [], {"other-model": {}}])):
            for value in values:
                with self.subTest(field=field, value=value):
                    event = {"type": "result", "result": "yes", "modelUsage": {
                        "claude-test": {}}, "total_cost_usd": 0.1, field: value}
                    row, _ = self.run_case(event)
                    self.assertEqual(row["status"], "error")

    def test_nontext_result_is_error(self):
        row, _ = self.run_case({"type": "result", "result": ["yes"],
                               "modelUsage": {"claude-test": {}}, "total_cost_usd": 0.1})
        self.assertEqual(row["status"], "error")

    def test_explicit_result_does_not_fall_back_to_intermediate_text(self):
        for final in ("", None, [], {}, 0, False):
            with self.subTest(final=final):
                events = [{"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "yes"}]}},
                    {"type": "result", "result": final,
                     "modelUsage": {"claude-test": {}}, "total_cost_usd": 0.1}]
                row, _ = self.run_case(stdout="\n".join(map(json.dumps, events)))
                if final == "":
                    self.assertEqual(row["status"], "ok")
                    self.assertEqual(row["score"], 0.0)
                    self.assertEqual(row["output_chars"], 0)
                else:
                    self.assertEqual(row["status"], "error")

    def test_missing_result_text_can_use_streamed_text(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "yes"}]}},
            {"type": "result", "modelUsage": {"claude-test": {}}, "total_cost_usd": 0.1}]
        row, _ = self.run_case(stdout="\n".join(map(json.dumps, events)))
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["score"], 1.0)

    def test_invalid_tool_name_is_error_instead_of_scoring(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": None, "input": {}}]}},
            {"type": "result", "result": "yes", "modelUsage": {"claude-test": {}},
             "total_cost_usd": 0.1}]
        row, _ = self.run_case(stdout="\n".join(map(json.dumps, events)))
        self.assertEqual(row["status"], "error")


class ScheduleTests(unittest.TestCase):
    def run_main(self, args, rows=None):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [CASE]}))
            output = io.StringIO()
            with mock.patch.object(run_eval, "CASES", cases), \
                    mock.patch.object(run_eval, "RESULTS", root / "results"), \
                    mock.patch.object(run_eval, "RUNDIR", root / "runs"), \
                    mock.patch.object(run_eval, "build_eval_settings") as settings, \
                    mock.patch.object(run_eval, "run_case", side_effect=rows) as run, \
                    mock.patch("sys.argv", ["run_eval.py", *args]), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                try:
                    rc = run_eval.main()
                except SystemExit as exc:
                    rc = exc.code
            saved = root / "results" / "latest.json"
            return rc, run, settings, json.loads(saved.read_text()) if saved.exists() else None

    def test_reject_invalid_arguments_before_settings_or_cli(self):
        for args in (["--trials", "0"], ["--timeout", "0"], ["--budget", "nan"],
                     ["--budget", "inf"], ["--budget", "-1"], ["--budget", "0.001"],
                     ["--matrix", "opus:high,opus:high"], ["--config", ":high"],
                     ["--config", "opus:high", "--matrix", "opus:low"],
                     ["--ids", "missing"], ["--extra-args=--max-budget-usd=99"],
                     ["--extra-args=--settings=fixture.json"], ["--extra-args='"]):
            with self.subTest(args=args):
                rc, run, settings, _ = self.run_main(args)
                self.assertEqual(rc, 2)
                run.assert_not_called()
                settings.assert_not_called()

    def test_each_call_checks_remaining_budget(self):
        row = {"id": "sample", "trial": 0, "status": "ok", "score": 1.0,
               "partial": 1.0, "cost": 0.995, "elapsed": 0.1}
        rc, run, _, out = self.run_main(["--matrix", "a:high,b:high", "--budget", "1"],
                                       [row, row])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(out["truncated"])

    def test_unknown_cost_stops_following_calls(self):
        row = {"id": "sample", "trial": 0, "status": "error", "score": None,
               "cost": None, "cost_unknown": True, "elapsed": 0.1, "reason": "timeout"}
        _, run, _, out = self.run_main(["--matrix", "a:high,b:high", "--budget", "2"],
                                      [row, row])
        self.assertEqual(run.call_count, 1)
        self.assertTrue(out["truncated"])

    def test_extra_arguments_preserve_quoted_values(self):
        row = {"id": "sample", "trial": 0, "status": "skip", "reason": "fixture",
               "score": None, "cost": 0, "elapsed": 0}
        _, run, _, _ = self.run_main(["--extra-args=--append-system-prompt 'two words'"], [row])
        self.assertEqual(run.call_args.args[4], ["--append-system-prompt", "two words"])

    def test_report_shows_known_cost_without_successful_cases(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                output = io.StringIO()
                rows = [{"id": "sample", "trial": 0, "status": "error", "cost": 0.25}]
                if unknown:
                    rows.append({"id": "sample", "trial": 1, "status": "error",
                                 "cost": None, "cost_unknown": True})
                with contextlib.redirect_stdout(output):
                    run_eval.render({"configs": {"opus:high": rows}})
                self.assertIn("0.250$" + ("+?" if unknown else ""), output.getvalue())


class ScorerTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires named pipes")
    def test_file_content_assertions_reject_fifos_without_reading(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            os.mkfifo(cwd / "pipe")
            for kind in ("file_matches", "file_not_matches"):
                with self.subTest(kind=kind), \
                        mock.patch.object(pathlib.Path, "read_text", return_value="fixture") as read:
                    passed, failed = run_eval.score({"assertions": [
                        {"type": kind, "path": "pipe", "re": "fixture"}]}, "", [], {}, cwd)
                    self.assertEqual(passed, [])
                    self.assertEqual(len(failed), 1)
                    read.assert_not_called()

    def test_zero_turns_are_valid(self):
        passed, failed = run_eval.score({"assertions": [{"type": "max_turns", "n": 1}]},
                                        "", [], {"num_turns": 0}, pathlib.Path("."))
        self.assertEqual(len(passed), 1)
        self.assertEqual(failed, [])

    def test_invalid_turn_count_fails_instead_of_crashing(self):
        for turns in (None, -1, "1", True):
            with self.subTest(turns=turns):
                passed, failed = run_eval.score(
                    {"assertions": [{"type": "max_turns", "n": 1}]},
                    "", [], {"num_turns": turns}, pathlib.Path("."))
                self.assertEqual(passed, [])
                self.assertEqual(len(failed), 1)

    def test_file_assertions_reject_directories_and_external_paths(self):
        with tempfile.TemporaryDirectory() as td:
            parent = pathlib.Path(td)
            cwd = parent / "work"
            cwd.mkdir()
            (parent / "outside.txt").write_text("fixture")
            (cwd / "link.txt").symlink_to(parent / "outside.txt")
            for path in (".", "../outside.txt", str(parent / "outside.txt"), "link.txt"):
                for kind in ("file_exists", "file_matches", "file_not_matches", "python_check"):
                    with self.subTest(path=path, kind=kind):
                        a = {"type": kind, "path": path, "re": "fixture", "code": "pass"}
                        with mock.patch.object(run_eval.subprocess, "run") as child:
                            passed, failed = run_eval.score({"assertions": [a]}, "", [], {}, cwd)
                        self.assertEqual(passed, [])
                        self.assertEqual(len(failed), 1)
                        child.assert_not_called()

    def test_probe_files_cannot_escape_temporary_directory(self):
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / "outside.txt"
            with self.assertRaises(ValueError):
                selfcheck.judge([], {"files": {str(target): "fixture"}}, "")
            self.assertFalse(target.exists())

    def test_python_check_does_not_inherit_credentials_or_home(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = pathlib.Path(td)
            (cwd / "answer.py").write_text("value = 42")
            with mock.patch.dict(os.environ, {"CC_EVAL_TEST_TOKEN": "fixture"}), \
                    mock.patch.object(run_eval.subprocess, "run") as child:
                child.return_value.returncode = 0
                self.assertTrue(run_eval._python_check(cwd, {"path": "answer.py", "code": "pass"}))
            env = child.call_args.kwargs["env"]
            self.assertNotIn("CC_EVAL_TEST_TOKEN", env)
            self.assertNotEqual(env["HOME"], os.environ.get("HOME"))


class SettingsTests(unittest.TestCase):
    def test_settings_replace_symlink_without_overwriting_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            source = root / "settings-fixture.json"
            source.write_text(json.dumps({"hooks": {"Stop": [{}]}, "env": {"TEST": "value"},
                                          "model": "opus", "statusLine": {}}))
            victim = root / "untouched.txt"
            victim.write_text("keep")
            victim.chmod(0o644)
            output = root / "eval-settings.json"
            output.symlink_to(victim)
            replace = os.replace

            def check_private_before_publish(src, dst):
                self.assertEqual(pathlib.Path(src).stat().st_mode & 0o777, 0o600)
                replace(src, dst)

            with mock.patch.object(run_eval, "REAL_SETTINGS", source), \
                    mock.patch.object(run_eval, "EVAL_SETTINGS", output), \
                    mock.patch.object(run_eval.os, "replace", side_effect=check_private_before_publish):
                self.assertEqual(run_eval.build_eval_settings(), output)
            self.assertFalse(output.is_symlink())
            self.assertEqual(victim.read_text(), "keep")
            self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output.read_text()), {"hooks": {"Stop": []}, "model": "opus"})

    def test_failed_settings_publish_preserves_old_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            source = root / "settings-fixture.json"
            source.write_text("{}")
            output = root / "eval-settings.json"
            output.write_text("previous")
            before = set(root.iterdir())
            with mock.patch.object(run_eval, "REAL_SETTINGS", source), \
                    mock.patch.object(run_eval, "EVAL_SETTINGS", output), \
                    mock.patch.object(run_eval.os, "replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    run_eval.build_eval_settings()
            self.assertEqual(output.read_text(), "previous")
            self.assertEqual(set(root.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
