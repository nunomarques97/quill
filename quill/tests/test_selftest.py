"""The typing self-test refuses to run without its flag and reports counts only.

Every way the self-test could reach the desktop (the Win32 layer, child
processes, threads, hooks, the page server) is replaced by a recorder that
fails the test if it is used.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from quill.selftest import typing as selftest


class DesktopRecorder(contextlib.ExitStack):
    """Patches every desktop entry point; ``used`` lists the ones that were reached."""

    def __init__(self) -> None:
        super().__init__()
        self.used: list[str] = []

    def _trap(self, name: str) -> mock.Mock:
        return mock.Mock(side_effect=lambda *args, **kwargs: self.used.append(name))

    def __enter__(self) -> DesktopRecorder:
        super().__enter__()
        targets = {
            "win32 layer": (selftest, "SelftestUser32"),
            "process": (subprocess, "Popen"),
            "process run": (subprocess, "run"),
            "dll": (ctypes, "WinDLL"),
            "thread": (threading.Thread, "start"),
            "page server": (selftest, "ThreadingHTTPServer"),
            "input hook": (selftest, "PhysicalInputMonitor"),
            "console reader": (selftest, "console_reader"),
            "runner": (selftest, "run"),
        }
        for name, (owner, attribute) in targets.items():
            self.enter_context(mock.patch.object(owner, attribute, self._trap(name)))
        return self


def run_main(*args: str) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            code = selftest.main(list(args))
        except SystemExit as exc:
            code = int(exc.code)
    return code, err.getvalue()


class RefusalTest(unittest.TestCase):
    def test_without_flag_exits_two_and_opens_nothing(self) -> None:
        for args in ((), ("--targets", "edit"), ("--targets", "edit,console,vscode")):
            with DesktopRecorder() as desktop:
                code, err = run_main(*args)
            self.assertEqual(code, 2, args)
            self.assertIn("--allow-desktop-input", err)
            self.assertEqual(desktop.used, [], args)

    def test_hidden_console_reader_also_needs_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as folder, DesktopRecorder() as desktop:
            code, _ = run_main("--console-reader", folder, "--nonce", "abc")
            self.assertEqual(os_listdir(folder), [])
        self.assertEqual((code, desktop.used), (2, []))

    def test_unknown_target_is_a_usage_error_before_any_window(self) -> None:
        with DesktopRecorder() as desktop:
            code, _ = run_main("--allow-desktop-input", "--targets", "edit,notepad")
        self.assertEqual((code, desktop.used), (2, []))

    def test_with_flag_the_runner_is_reached(self) -> None:
        with DesktopRecorder() as desktop:
            run_main("--allow-desktop-input", "--targets", "edit")
        self.assertEqual(desktop.used, ["runner"])
        with mock.patch.object(selftest, "run", return_value=0) as runner:
            self.assertEqual(run_main("--allow-desktop-input", "--targets", " edit , console ")[0], 0)
        runner.assert_called_once_with(["edit", "console"])

    def test_console_target_passes_the_flag_to_its_reader(self) -> None:
        source = Path(selftest.__file__).read_text("utf-8")
        start = source.index("class ConsoleTarget")
        self.assertIn('"--allow-desktop-input"', source[start:source.index("def console_reader")])


def os_listdir(folder: str) -> list[str]:
    return sorted(path.name for path in Path(folder).iterdir())


class AggregateTest(unittest.TestCase):
    def outcomes(self) -> list[selftest.TargetOutcome]:
        passed = selftest.TargetOutcome("edit", ok=True, attempts=1, cases=[
            selftest.CaseResult("edit", case.name, True, len(case.text), len(case.text), "", 0.5)
            for case in selftest.CORPUS
        ])
        lost, extra, changed = selftest.diff_counts("abcdef", "abXdefg")
        failed = selftest.TargetOutcome("console", ok=False, attempts=2, clipboard_identical=False, cases=[
            selftest.CaseResult("console", "short", False, 6, 6, "mismatch: counts", 0.2, lost, extra, changed),
        ])
        return [passed, failed]

    def test_diff_counts(self) -> None:
        self.assertEqual(selftest.diff_counts("abcdef", "abcdef"), (0, 0, 0))
        self.assertEqual(selftest.diff_counts("abcdef", "abdef"), (1, 0, 0))
        self.assertEqual(selftest.diff_counts("abcdef", "abccdef"), (0, 1, 0))
        self.assertEqual(selftest.diff_counts("abcdef", "abXdef"), (0, 0, 1))

    def test_compare_reports_counts_not_text(self) -> None:
        detail = selftest.compare("texto esperado", "texto recebido")
        self.assertIn("mismatch", detail)
        self.assertNotIn("esperado", detail)
        self.assertNotIn("recebido", detail)

    def test_result_file_has_aggregates_and_never_typed_text(self) -> None:
        finished = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        data = selftest.aggregate(self.outcomes(), finished)
        with tempfile.TemporaryDirectory() as folder:
            path = selftest.write_result(data, Path(folder) / "selftest", finished)
            self.assertEqual(path.name, "typing-20260102T030405Z.json")
            text = path.read_text("utf-8")
            self.assertEqual(os_listdir(str(path.parent)), [path.name])
        stored = json.loads(text)
        self.assertFalse(stored["passed"])
        self.assertEqual(stored["totals"]["targets"], 2)
        self.assertEqual(stored["totals"]["targets_passed"], 1)
        self.assertEqual(stored["totals"]["clipboard_changed_targets"], 1)
        self.assertEqual((stored["totals"]["lost"], stored["totals"]["extra"], stored["totals"]["changed"]),
                         (0, 1, 1))
        self.assertEqual(stored["targets"][1]["attempts"], 2)
        for case in selftest.CORPUS:
            for word in case.text.split():
                if len(word) >= 5:
                    self.assertNotIn(word, text)

    def test_results_folder_is_inside_ignored_local(self) -> None:
        self.assertEqual(selftest.RESULTS_DIR, selftest.REPO_ROOT / "local" / "selftest")


if __name__ == "__main__":
    unittest.main()
