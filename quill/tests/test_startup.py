"""quill.startup tests with a fake registry: the real Run key is never read or written."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quill import startup
from quill.config import REPO_ROOT
from quill.tests.fakes import FakeRegistry


def _forbidden(*args, **kwargs):
    raise AssertionError("tests must use a fake registry, never the real Run key")


def fake_repo(root: Path, pythonw: bool = True, script: bool = True) -> Path:
    if pythonw:
        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
    if script:
        (root / "quill").mkdir(parents=True)
        (root / "quill" / "__main__.py").write_text("", encoding="utf-8")
    return root


class Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(startup.WinRegistry, "__init__", _forbidden)
        patcher.start()
        self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = fake_repo(Path(folder.name) / "repo folder")
        self.registry = FakeRegistry()


class LaunchCommandTest(Case):
    def test_pythonw_and_the_entry_script_of_the_repository_both_quoted(self):
        root = self.root.resolve()
        self.assertEqual(startup.launch_command(self.root),
                         f'"{root / ".venv" / "Scripts" / "pythonw.exe"}" "{root / "quill" / "__main__.py"}"')

    def test_resolved_at_runtime_from_this_checkout(self):
        self.assertEqual(startup.ENTRY_SCRIPT, REPO_ROOT / "quill" / "__main__.py")
        self.assertEqual(startup.VENV_DIR, REPO_ROOT / ".venv")
        self.assertTrue(startup.ENTRY_SCRIPT.is_file())

    def test_missing_pythonw_or_script_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(startup.StartupError, "pythonw"):
                startup.launch_command(fake_repo(Path(folder) / "a", pythonw=False))
            with self.assertRaisesRegex(startup.StartupError, "__main__"):
                startup.launch_command(fake_repo(Path(folder) / "b", script=False))

    def test_a_quote_in_the_path_is_refused(self):
        with mock.patch.object(Path, "resolve", lambda self: self):
            with self.assertRaises(startup.StartupError):
                startup.launch_command(Path('C:/invented"folder'))


class RegistryTest(Case):
    def test_install_status_and_remove(self):
        self.assertEqual(startup.status(self.registry, self.root), startup.ABSENT)
        self.assertEqual(startup.install(self.registry, self.root), startup.ABSENT)
        self.assertEqual(self.registry.values, {"Quill": startup.launch_command(self.root)})
        self.assertEqual(startup.status(self.registry, self.root), startup.INSTALLED)
        # Installing again changes nothing.
        self.assertEqual(startup.install(self.registry, self.root), startup.INSTALLED)
        self.assertEqual(len(self.registry.writes), 1)
        self.assertTrue(startup.remove(self.registry))
        self.assertEqual(self.registry.values, {})
        self.assertFalse(startup.remove(self.registry))

    def test_an_entry_pointing_elsewhere_is_reported_and_replaced(self):
        self.registry.values = {"Quill": '"C:\\invented\\pythonw.exe" "C:\\invented\\old.py"', "Other": "kept"}
        self.assertEqual(startup.status(self.registry, self.root), startup.OTHER)
        self.assertEqual(startup.install(self.registry, self.root), startup.OTHER)
        self.assertEqual(self.registry.values["Quill"], startup.launch_command(self.root))
        self.assertEqual(self.registry.values["Other"], "kept")

    def test_only_the_quill_value_is_touched(self):
        self.registry.values = {"Other": "kept"}
        startup.install(self.registry, self.root)
        startup.remove(self.registry)
        self.assertEqual(self.registry.values, {"Other": "kept"})

    def test_nothing_is_written_when_the_repository_is_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            lines = []
            code = startup.install_command(self.registry, fake_repo(Path(folder) / "r", pythonw=False),
                                           out=lines.append)
        self.assertEqual(code, 1)
        self.assertEqual(self.registry.writes, [])
        self.assertIn("not set", lines[0])

    def test_commands_report_each_case(self):
        lines = []
        self.assertEqual(startup.install_command(self.registry, self.root, out=lines.append), 0)
        self.assertEqual(startup.install_command(self.registry, self.root, out=lines.append), 0)
        self.assertEqual(startup.remove_command(self.registry, out=lines.append), 0)
        self.assertEqual(startup.remove_command(self.registry, out=lines.append), 0)
        self.assertEqual(len(lines), 4)
        self.assertIn("will start with Windows", lines[0])
        self.assertIn("already", lines[1])
        self.assertIn("no longer", lines[2])
        self.assertIn("was not set", lines[3])

    def test_registry_errors_are_reported(self):
        self.registry.fail = PermissionError("fake")
        lines = []
        self.assertEqual(startup.install_command(self.registry, self.root, out=lines.append), 1)
        self.assertEqual(startup.remove_command(self.registry, out=lines.append), 1)
        self.assertEqual(len(lines), 2)


class ScriptTest(unittest.TestCase):
    def test_the_entry_script_runs_from_any_folder(self):
        # What the Run entry does, minus pythonw and the app itself: --help only.
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(startup.ENTRY_SCRIPT), "--help"], cwd=folder,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--install-startup", result.stdout)


if __name__ == "__main__":
    unittest.main()
