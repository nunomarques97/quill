"""quill.projects tests: invented titles, names and folders in temporary directories, a fake process table.

No real window, process or process memory is touched: the process reader is
``fakes.FakeProcesses``, shortcut files are synthetic, and the drive type is
a fake.
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from quill import projects as P
from quill.app import TextPipeline
from quill.autorewrite import project_hint
from quill.config import load_config
from quill.inject import Target
from quill.profiles import WindowInfo
from quill.shortcuts import LinkTarget
from quill.tests.fakes import FakeProcesses
from quill.tests.test_shortcuts import write_link
from quill.vocabulary import Entry, Vocabulary
from quill.win32 import INTEGRITY_HIGH, INTEGRITY_MEDIUM

CODE = "C:\\Tools\\Microsoft VS Code\\Code.exe"
APP = "Visual Studio Code"


def fixed(drive: str) -> int:
    return P.DRIVE_FIXED


def code(title: str) -> WindowInfo:
    return WindowInfo("Code.exe", "Chrome_WidgetWin_1", title)


def terminal(title: str = "Claude Code") -> WindowInfo:
    return WindowInfo("WindowsTerminal.exe", "CASCADIA_HOSTING_WINDOW_CLASS", title)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(os.path.realpath(folder.name))

    def folder(self, *parts: str, git: bool = False) -> Path:
        path = self.base.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        if git:
            (path / ".git").mkdir()
        return path


# ---------------------------------------------------------------- titles


class VscodeTitleTest(unittest.TestCase):
    def test_hub_titles_give_the_project_only(self) -> None:
        for title in (f"alpha-app | notes.md - {APP} [Claude Code]", f"alpha-app | notes.md - {APP}",
                      f"alpha-app | {APP} [Claude Code]", f"alpha-app | {APP}",
                      f"alpha-app | draft - v2.md - {APP} [Explorer]", f"● alpha-app | notes.md - {APP} [Search]",
                      f"  alpha-app   |   notes.md  -  {APP}  [Claude Code]  "):
            with self.subTest(title=title):
                self.assertEqual(P.vscode_project(title), "alpha-app")
        self.assertEqual(P.vscode_project(f"Beta Site | index.html - {APP} [Claude Code]"), "Beta Site")

    def test_standard_titles_still_give_the_folder(self) -> None:
        for title in (f"main.py - nimbus - Work - {APP} [Claude Code]", f"main.py - nimbus - {APP}",
                      f"● main.py - nimbus - {APP}", f"main.py ● - nimbus - {APP} [Explorer]",
                      f"nimbus - {APP}", f"nimbus (Workspace) - {APP}", f"nimbus - {APP} [Administrator]"):
            with self.subTest(title=title):
                self.assertEqual(P.vscode_project(title), "nimbus")

    def test_titles_without_a_project(self) -> None:
        for title in ("", "   ", APP, f"{APP} [Claude Code]", f"README.md - {APP}", f"● notes.txt - {APP}",
                      "nimbus - Notepad", f"nimbus - {APP} Insiders", f"x{APP}", f" | {APP}",
                      f"main.py - {'n' * 61} - {APP}", f"{'n' * 61} | a.md - {APP}",
                      f"main.py - nim\u0007bus - {APP}", f"nim\u200ebus | a.md - {APP}",
                      "a" * (P.MAX_TITLE_CHARS + 1) + f" - {APP}", None, 42):
            with self.subTest(title=title):
                self.assertIsNone(P.vscode_project(title))

    def test_names_in_a_title(self) -> None:
        names = ["alpha", "alpha-app", "Órbita", "beta"]
        self.assertEqual(P.names_in_title("✳ Claude Code - ALPHA APP", names), ["alpha-app"])
        self.assertEqual(P.names_in_title("orbita: fix build", names), ["Órbita"])
        self.assertEqual(P.names_in_title("alphabet soup", names), [])
        self.assertEqual(P.names_in_title("alpha and beta", names), ["alpha", "beta"])
        self.assertEqual(P.names_in_title("a" * (P.MAX_TITLE_CHARS + 1), names), [])


# ---------------------------------------------------------------- paths


class GitRootTest(Case):
    def test_the_nearest_git_root(self) -> None:
        root = self.folder("work", "alpha-app", git=True)
        deep = self.folder("work", "alpha-app", "src", "core")
        self.assertEqual(P.git_root(str(deep), drives=fixed), str(root))
        self.assertEqual(P.git_root(str(deep) + "\\", drives=fixed), str(root))
        self.assertEqual(P.git_root(str(root), drives=fixed), str(root))
        # A worktree has a .git file.
        worktree = self.folder("work", "alpha-tree")
        (worktree / ".git").write_text("gitdir: elsewhere", "utf-8")
        self.assertEqual(P.git_root(str(worktree), drives=fixed), str(worktree))

    def test_no_git_root(self) -> None:
        plain = self.folder("plain", "notes")
        seen: list[str] = []

        def exists(path: str) -> bool:
            seen.append(path)
            return False

        self.assertIsNone(P.git_root(str(plain), exists=exists, drives=fixed))
        # The drive root is never checked, and the walk is bounded.
        self.assertTrue(seen)
        self.assertNotIn(os.path.splitdrive(str(plain))[0] + "\\.git", seen)
        deep = "C:\\" + "\\".join(f"d{index}" for index in range(P.MAX_DEPTH + 10))
        seen.clear()
        self.assertIsNone(P.git_root(deep, exists=exists, drives=fixed))
        self.assertEqual(len(seen), P.MAX_DEPTH)

    def test_network_unc_device_and_relative_paths_are_never_probed(self) -> None:
        def exists(path: str) -> bool:
            raise AssertionError("probed")

        for path in ("\\\\server\\share\\alpha", "//server/share/alpha", "\\\\?\\C:\\alpha", "\\\\.\\C:\\alpha",
                     "alpha\\src", "\\alpha", "C:alpha", "", "C:\\alp\0ha", "C:\\" + "a" * P.MAX_PATH_CHARS, None):
            with self.subTest(path=path):
                self.assertIsNone(P.git_root(path, exists=exists, drives=fixed))
        self.assertIsNone(P.git_root("Z:\\alpha", exists=exists, drives=lambda drive: 4))  # a network drive
        self.assertIsNone(P.git_root("Z:\\alpha", exists=exists, drives=lambda drive: 1))  # no such drive

    def test_local_folder(self) -> None:
        alpha = self.folder("alpha")
        self.assertEqual(P.local_folder(alpha, drives=fixed), str(alpha))
        self.assertIsNone(P.local_folder(self.base / "absent", drives=fixed))
        (self.base / "file.txt").write_text("x", "utf-8")
        self.assertIsNone(P.local_folder(self.base / "file.txt", drives=fixed))
        self.assertIsNone(P.local_folder(os.path.splitdrive(str(alpha))[0] + "\\", drives=fixed))
        self.assertIsNone(P.local_folder(alpha, drives=lambda drive: 4))
        self.assertIsNone(P.local_folder("\\\\server\\share\\alpha", is_dir=lambda path: True, drives=fixed))


class ArgumentsTest(unittest.TestCase):
    def test_windows_argument_rules(self) -> None:
        self.assertEqual(P.split_arguments('--new-window "C:\\Hub\\a b.code-workspace"'),
                         ["--new-window", "C:\\Hub\\a b.code-workspace"])
        self.assertEqual(P.split_arguments('"C:\\a b\\\\" x'), ["C:\\a b\\", "x"])
        self.assertEqual(P.split_arguments('a\\\\\\"b "c""d" ""'), ['a\\"b', 'c"d', ""])
        self.assertEqual(P.split_arguments("  C:\\x\\y  \t z "), ["C:\\x\\y", "z"])
        self.assertEqual(P.split_arguments(""), [])


class WorkspaceTest(Case):
    def workspace(self, text: str, name: str = "alpha.code-workspace", encoding: str = "utf-8") -> str:
        path = self.folder("hub", "_config") / name
        path.write_text(text, encoding)
        return str(path)

    def test_first_folder_with_comments_and_trailing_commas(self) -> None:
        alpha = self.folder("repos", "alpha-app")
        beta = self.folder("repos", "beta")
        path = self.workspace('{\n  // the project\n  "folders": [\n    /* first */ {"name": "a // b", "path": "'
                              + str(alpha).replace("\\", "\\\\") + '",},\n    {"path": "' +
                              str(beta).replace("\\", "\\\\") + '"},\n  ],\n  "settings": {"x": "/* no */",},\n}\n',
                              encoding="utf-8-sig")
        self.assertEqual(P.workspace_folder(path, drives=fixed), str(alpha))

    def test_a_relative_folder_is_resolved_from_the_workspace_file(self) -> None:
        alpha = self.folder("hub", "alpha-app")
        path = self.workspace('{"folders": [{"path": "../alpha-app"}]}')
        self.assertEqual(P.workspace_folder(path, drives=fixed), str(alpha))
        path = self.workspace('{"folders": [{"path": "."}]}', "self.code-workspace")
        self.assertEqual(P.workspace_folder(path, drives=fixed), str(self.base / "hub" / "_config"))

    def test_unusable_workspaces(self) -> None:
        self.folder("hub", "alpha-app")
        for text in ('{"folders": []}', '{"folders": [{"uri": "file:///C:/x"}]}', '{"folders": "x"}', "[]",
                     "{not json", '{"folders": [{"path": "\\\\\\\\server\\\\share"}]}',
                     '{"folders": [{"path": "C:relative"}]}', '{"folders": [{"path": "../absent"}]}',
                     '{"folders": [{"path": 3}]}', '{"folders": [{"path": "../alpha-app"}]}' + " " * P.MAX_WORKSPACE_BYTES):
            with self.subTest(text=text[:60]):
                self.assertIsNone(P.workspace_folder(self.workspace(text), drives=fixed))
        (self.base / "hub" / "_config" / "bad.code-workspace").write_bytes(b'{"folders": [{"path": "\xff"}]}')
        self.assertIsNone(P.workspace_folder(str(self.base / "hub" / "_config" / "bad.code-workspace"),
                                             drives=fixed))
        self.assertIsNone(P.workspace_folder(str(self.base / "absent.code-workspace"), drives=fixed))
        self.assertIsNone(P.workspace_folder("\\\\server\\share\\a.code-workspace", drives=fixed))


# ---------------------------------------------------------------- names to folders


class Stamps:
    """A fake ``os.stat``: a stamp per path, changed by ``touch``."""

    def __init__(self) -> None:
        self.stamps: dict[str, int] = {}

    def touch(self, path: object) -> None:
        self.stamps[os.fspath(path)] = self.stamps.get(os.fspath(path), 0) + 1

    def __call__(self, path: object) -> SimpleNamespace:
        stamp = self.stamps.get(os.fspath(path), 0)
        return SimpleNamespace(st_mtime_ns=stamp, st_size=0)


class FoldersTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.hub = self.folder("hub")
        self.targets: dict[str, LinkTarget | None] = {}
        self.reads = 0
        self.stamps = Stamps()

    def link(self, name: str, target: LinkTarget | None) -> None:
        (self.hub / f"{name}.lnk").write_bytes(b"")
        self.targets[name] = target

    def reader(self, path: Path) -> LinkTarget | None:
        self.reads += 1
        return self.targets.get(path.name[: -len(".lnk")])

    def folders(self, configured: tuple = (), **options: object) -> P.ProjectFolders:
        return P.ProjectFolders(configured, (self.hub,), **{"reader": self.reader, "drives": fixed,
                                                            "stat": self.stamps, **options})

    def test_shortcut_targets_name_folders(self) -> None:
        alpha, beta, gamma = self.folder("r", "alpha-app"), self.folder("r", "beta"), self.folder("r", "gamma")
        workspace = self.folder("hub", "_config") / "gamma.code-workspace"
        workspace.write_text('{"folders": [{"path": "' + str(gamma).replace("\\", "\\\\") + '"}]}', "utf-8")
        self.link("alpha-app", LinkTarget(str(alpha), directory=True))
        self.link("Beta", LinkTarget(CODE, f'--new-window "{beta}"'))
        self.link("Gamma Site", LinkTarget(CODE, f'--new-window "{workspace}"'))
        self.link("notes", LinkTarget("C:\\Windows\\notepad.exe", str(alpha)))
        self.link("broken", None)
        folders = self.folders()
        self.assertEqual(folders.folder_for("alpha-app"), str(alpha))
        self.assertEqual(folders.folder_for("ALPHA APP"), str(alpha))
        self.assertEqual(folders.folder_for("béta"), str(beta))
        self.assertEqual(folders.folder_for("gamma-site"), str(gamma))
        self.assertEqual(folders.folder_for("gamma"), str(gamma))  # the folder's own name
        self.assertIsNone(folders.folder_for("notes"))
        self.assertIsNone(folders.folder_for("broken"))
        self.assertIsNone(folders.folder_for("delta"))
        self.assertIsNone(folders.folder_for("--"))
        self.assertEqual(folders.name_for(str(gamma)), "Gamma Site")
        self.assertIsNone(folders.name_for(str(self.base)))
        self.assertEqual(sorted(folders.names()), ["Beta", "Gamma Site", "alpha-app"])

    def test_the_configured_folders_come_first(self) -> None:
        alpha, other = self.folder("r", "alpha-app"), self.folder("r", "alpha-old")
        self.link("alpha-app", LinkTarget(str(other), directory=True))
        folders = self.folders((("Alpha App", alpha),))
        self.assertEqual(folders.folder_for("alpha-app"), str(alpha))
        self.assertEqual(folders.name_for(str(alpha)), "Alpha App")
        # A configured folder that is missing gives no folder (never the shortcut's).
        folders = self.folders((("alpha-app", self.base / "absent"),))
        self.assertIsNone(folders.folder_for("alpha-app"))

    def test_ambiguous_network_and_missing_folders_give_none(self) -> None:
        one, two = self.folder("r", "one"), self.folder("r2", "two")
        self.link("alpha", LinkTarget(str(one), directory=True))
        self.link("Alpha!", LinkTarget(str(two), directory=True))
        self.link("same", LinkTarget(str(one), directory=True))
        self.link("Same.", LinkTarget(CODE, f'"{one}"'))
        self.link("unc", LinkTarget("\\\\server\\share\\unc", directory=True))
        self.link("device", LinkTarget("\\\\?\\C:\\device", directory=True))
        self.link("gone", LinkTarget(str(self.base / "gone"), directory=True))
        self.link("root", LinkTarget("C:\\", directory=True))
        folders = self.folders()
        self.assertIsNone(folders.folder_for("alpha"))
        self.assertEqual(folders.folder_for("same"), str(one))
        for name in ("unc", "device", "gone", "root"):
            self.assertIsNone(folders.folder_for(name), name)
        # Two configured names that compare equal but point to different folders.
        self.assertIsNone(self.folders((("delta", one), ("Delta", two))).folder_for("delta"))
        # A folder removed after the map was built.
        (self.base / "r2" / "two").rmdir()
        self.link("two", LinkTarget(str(two), directory=True))
        self.stamps.touch(self.hub)
        self.assertIsNone(self.folders().folder_for("two"))
        network = self.folders(drives=lambda drive: 4)
        self.assertIsNone(network.folder_for("same"))

    def test_the_map_is_cached_until_a_source_changes(self) -> None:
        alpha, beta = self.folder("r", "alpha"), self.folder("r", "beta")
        workspace = self.folder("hub", "_config") / "beta.code-workspace"
        workspace.write_text('{"folders": [{"path": "' + str(alpha).replace("\\", "\\\\") + '"}]}', "utf-8")
        self.link("beta", LinkTarget(CODE, f'--new-window "{workspace}"'))
        folders = self.folders()
        for _ in range(5):
            self.assertEqual(folders.folder_for("beta"), str(alpha))
        self.assertEqual((folders.builds, self.reads), (1, 1))
        # A new shortcut changes the folder: read again.
        self.link("gamma", LinkTarget(str(beta), directory=True))
        self.stamps.touch(self.hub)
        self.assertEqual(folders.folder_for("gamma"), str(beta))
        self.assertEqual((folders.builds, self.reads), (2, 3))
        # A changed workspace file: read again.
        workspace.write_text('{"folders": [{"path": "' + str(beta).replace("\\", "\\\\") + '"}]}', "utf-8")
        self.stamps.touch(workspace)
        self.assertEqual(folders.folder_for("beta"), str(beta))
        self.assertEqual(folders.builds, 3)

    def test_real_shortcut_files(self) -> None:
        alpha = self.folder("r", "alpha-app")
        workspace = self.folder("hub", "_config") / "alpha-app.code-workspace"
        workspace.write_text('{"folders": [{"path": "../../r/alpha-app"}]}', "utf-8")
        write_link(self.hub, "alpha-app", CODE, arguments=f'--new-window "{workspace}"')
        folders = P.ProjectFolders((), (self.hub,), drives=fixed)
        self.assertEqual(folders.folder_for("alpha app"), str(alpha))
        self.assertEqual(folders.folder_for("alpha app"), str(alpha))
        self.assertEqual(folders.builds, 1)

    def test_no_shortcut_folders(self) -> None:
        alpha = self.folder("alpha")
        folders = P.ProjectFolders((("alpha", alpha),), (), drives=fixed)
        self.assertEqual(folders.folder_for("alpha"), str(alpha))
        self.assertEqual((folders.names(), folders.builds), (["alpha"], 0))


# ---------------------------------------------------------------- Claude Code sessions


WT, SHELL, CLAUDE, OTHER_SHELL = 10, 11, 12, 13


def table(**extra: tuple[int, str, int | None]) -> dict[int, tuple[int, str, int | None]]:
    rows = {WT: (1, "WindowsTerminal.exe", 100), SHELL: (WT, "pwsh.exe", 110), CLAUDE: (SHELL, "claude.exe", 120),
            OTHER_SHELL: (WT, "cmd.exe", 130), 14: (WT, "OpenConsole.exe", 105), 1: (0, "explorer.exe", 50)}
    rows.update({int(pid[1:]): row for pid, row in extra.items()})
    return rows


class SessionTest(unittest.TestCase):
    def reader(self, **extra: tuple[int, str, int | None]) -> FakeProcesses:
        return FakeProcesses(table(**extra), {CLAUDE: "C:\\Work\\alpha\\src\\", 22: "C:\\Work\\beta\\"})

    def test_one_session_gives_its_directory(self) -> None:
        reader = self.reader()
        self.assertEqual(P.claude_directory(reader, WT), ("C:\\Work\\alpha\\src\\", P.SESSION))
        # A Claude Code started inside the session (a worker, a subagent) is part of it.
        reader = self.reader(p20=(CLAUDE, "node.exe", 125), p21=(20, "Claude.EXE", 126))
        self.assertEqual(P.claude_directory(reader, WT)[1], P.SESSION)
        self.assertNotIn(("current_directory", 21), reader.calls)

    def test_no_or_several_sessions_give_nothing(self) -> None:
        reader = self.reader(p22=(OTHER_SHELL, "claude.exe", 140))
        self.assertEqual(P.claude_directory(reader, WT), (None, P.SESSIONS))
        self.assertFalse([call for call in reader.calls if call[0] == "current_directory"])
        reader = FakeProcesses({WT: (1, "WindowsTerminal.exe", 100), SHELL: (WT, "pwsh.exe", 110)})
        self.assertEqual(P.claude_directory(reader, WT), (None, P.NO_SESSION))
        self.assertEqual(P.claude_directory(self.reader(), 0), (None, P.NO_SESSION))
        # Another window's session is not a descendant.
        self.assertEqual(P.claude_directory(self.reader(), 1)[1], P.SESSION)
        self.assertEqual(P.claude_directory(self.reader(), OTHER_SHELL), (None, P.NO_SESSION))

    def test_a_reused_parent_id_never_adopts_a_stranger(self) -> None:
        # A session older than the window's process only has a parent ID that was reused.
        reader = self.reader(p22=(WT, "claude.exe", 90))
        self.assertEqual(P.claude_directory(reader, WT), ("C:\\Work\\alpha\\src\\", P.SESSION))
        reader = FakeProcesses({WT: (1, "WindowsTerminal.exe", 100), 22: (WT, "claude.exe", 90)},
                               {22: "C:\\Work\\beta\\"})
        self.assertEqual(P.claude_directory(reader, WT), (None, P.NO_SESSION))

    def test_processes_that_cannot_be_verified_or_read(self) -> None:
        cases = {
            P.UNVERIFIED: lambda r: r.table.__setitem__(SHELL, (WT, "pwsh.exe", None)),
            P.OTHER_USER: lambda r: r.other_users.add(CLAUDE),
            P.ELEVATED: lambda r: r.integrity.__setitem__(CLAUDE, INTEGRITY_HIGH),
            P.UNREADABLE: lambda r: r.directories.__setitem__(CLAUDE, PermissionError("denied")),
            P.VANISHED: lambda r: r.vanish.add(CLAUDE),
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                reader = self.reader()
                change(reader)
                self.assertEqual(P.claude_directory(reader, WT), (None, reason))
                if reason in (P.OTHER_USER, P.ELEVATED, P.UNVERIFIED):
                    self.assertNotIn(("current_directory", CLAUDE), reader.calls)
        # Integrity that cannot be told counts as elevated; so does an unknown own integrity.
        reader = self.reader()
        reader.integrity[CLAUDE] = None
        self.assertEqual(P.claude_directory(reader, WT)[1], P.ELEVATED)
        reader = self.reader()
        reader.own = None
        self.assertEqual(P.claude_directory(reader, WT)[1], P.ELEVATED)
        reader = self.reader()
        reader.fail_list = OSError("snapshot")
        self.assertEqual(P.claude_directory(reader, WT), (None, P.LIST_FAILED))
        reader = self.reader()
        del reader.table[WT]
        self.assertEqual(P.claude_directory(reader, WT), (None, P.UNVERIFIED))  # the window's process is gone
        reader = self.reader()
        reader.directories[CLAUDE] = "C:\\" + "a" * P.MAX_PATH_CHARS
        self.assertEqual(P.claude_directory(reader, WT), (None, P.UNREADABLE))
        self.assertEqual(reader.own, INTEGRITY_MEDIUM)

    def test_the_walk_is_bounded(self) -> None:
        deep = {pid: (pid - 1, "cmd.exe", pid) for pid in range(101, 101 + P.MAX_TREE_DEPTH + 2)}
        deep[100] = (1, "WindowsTerminal.exe", 100)
        deep[200] = (101 + P.MAX_TREE_DEPTH + 1, "claude.exe", 1000)
        self.assertEqual(P.claude_directory(FakeProcesses(deep, {200: "C:\\x\\"}), 100), (None, P.TOO_MANY))
        wide = {pid: (100, "cmd.exe", pid) for pid in range(101, 101 + P.MAX_DESCENDANTS + 1)}
        wide[100] = (1, "WindowsTerminal.exe", 100)
        self.assertEqual(P.claude_directory(FakeProcesses(wide), 100), (None, P.TOO_MANY))


# ---------------------------------------------------------------- detection


class DetectorTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.alpha = self.folder("repos", "alpha-app", git=True)
        self.beta = self.folder("repos", "beta-site", git=True)
        self.folder("repos", "alpha-app", "src")
        self.hub = self.folder("hub")
        write_link(self.hub, "Alpha App", str(self.alpha), directory=True)
        self.processes = FakeProcesses(table(), {CLAUDE: str(self.alpha / "src") + "\\"})
        self.detector = P.ProjectDetector(P.ProjectFolders((("beta-site", self.beta),), (self.hub,), drives=fixed),
                                          self.processes)

    def detect(self, info: object, claude_code: bool = True) -> P.Project | None:
        return self.detector.detect(info, WT, claude_code=claude_code)

    def test_vscode_titles(self) -> None:
        found = self.detect(code(f"alpha-app | notes.md - {APP} [Claude Code]"))
        self.assertEqual((found.name, found.folder, found.source), ("alpha-app", self.alpha, P.VSCODE_TITLE))
        found = self.detect(code(f"main.py - beta-site - {APP}"), claude_code=False)
        self.assertEqual((found.name, found.folder), ("beta-site", self.beta))
        self.assertIsNone(self.detect(code(f"gamma | notes.md - {APP} [Claude Code]")))
        self.assertIsNone(self.detect(code(f"README.md - {APP}")))
        self.assertEqual(self.processes.calls, [])

    def test_a_known_name_in_a_terminal_title_wins(self) -> None:
        found = self.detect(terminal("✳ Claude Code · beta site"))
        self.assertEqual((found.name, found.folder, found.source), ("beta-site", self.beta, P.TERMINAL_TITLE))
        self.assertEqual(self.processes.calls, [])

    def test_the_terminal_session_folder(self) -> None:
        found = self.detect(terminal())
        self.assertEqual((found.name, found.folder, found.source), ("Alpha App", self.alpha, P.SESSION))
        # Without a shortcut the Git root's own name is the project.
        gamma = self.folder("repos", "gamma", git=True)
        self.processes.directories[CLAUDE] = str(gamma)
        found = self.detect(terminal())
        self.assertEqual((found.name, found.folder), ("gamma", gamma))
        # Two names in the title with different folders: the session decides.
        self.processes.directories[CLAUDE] = str(self.alpha)
        self.assertEqual(self.detect(terminal("alpha app and beta-site")).source, P.SESSION)

    def test_no_project(self) -> None:
        plain = self.folder("plain")
        self.processes.directories[CLAUDE] = str(plain)
        self.assertIsNone(self.detect(terminal()))  # no Git root
        self.processes.directories[CLAUDE] = "\\\\server\\share\\alpha"
        self.assertIsNone(self.detect(terminal()))
        self.processes.other_users.add(CLAUDE)
        self.assertIsNone(self.detect(terminal()))
        self.assertIsNone(self.detect(terminal(), claude_code=False))
        self.assertIsNone(self.detect(WindowInfo("chrome.exe", "Chrome_WidgetWin_1", "alpha app")))
        self.assertIsNone(self.detect(None))
        self.assertIsNone(P.ProjectDetector(self.detector.folders, None).detect(terminal(), WT, claude_code=True))

    def test_a_failure_gives_no_project(self) -> None:
        class Broken:
            def names(self):
                raise RuntimeError("broken")

        detector = P.ProjectDetector(Broken(), self.processes)  # type: ignore[arg-type]
        with self.assertLogs("quill.projects", logging.WARNING) as logs:
            self.assertIsNone(detector.detect(terminal(), WT, claude_code=True))
        self.assertIn("failed (RuntimeError)", logs.output[0])

    def test_logs_hold_reason_codes_and_counts_only(self) -> None:
        with self.assertLogs("quill.projects", logging.DEBUG) as logs:
            self.detect(code(f"alpha-app | notes.md - {APP} [Claude Code]"))
            self.detect(terminal("beta-site"))
            self.detect(terminal())
            self.detect(code(f"gamma | secretfile.md - {APP}"))
        text = "\n".join(logs.output).casefold()
        for secret in ("alpha", "beta", "gamma", "secretfile", str(self.base).casefold(), "repos", "hub"):
            self.assertNotIn(secret, text)
        self.assertIn(P.SESSION, text)


# ---------------------------------------------------------------- the text pipeline


class FixedDetector:
    def __init__(self, project: P.Project | None) -> None:
        self.project = project
        self.calls: list[tuple[object, int, bool]] = []

    def detect(self, info: object, pid: int = 0, *, claude_code: bool = False) -> P.Project | None:
        self.calls.append((info, pid, claude_code))
        return self.project


class PipelineTest(unittest.TestCase):
    TITLES = [(code(f"alpha-app | notes.md - {APP} [Claude Code]"), "claude-code"),
              (code(f"main.py - nimbus - {APP}"), "vscode"), (terminal("Claude Code - Orion"), "claude-code"),
              (WindowInfo("chrome.exe", "Chrome_WidgetWin_1", "Orion"), "default")]

    def pipeline(self, detector: object | None) -> TextPipeline:
        vocabulary = Vocabulary(names=(Entry("Orion", "name"),))
        info = {}

        def describe(target: Target) -> WindowInfo:
            return info["window"]

        pipeline = TextPipeline(load_config(None), vocabulary=vocabulary, generic_terms=(), describe=describe,
                                projects=detector)
        pipeline.window = info  # type: ignore[attr-defined]
        return pipeline

    def run_on(self, pipeline: TextPipeline, window: WindowInfo):
        pipeline.window["window"] = window  # type: ignore[attr-defined]
        return pipeline("abre o ficheiro", Target(hwnd=100, pid=WT))

    def test_without_a_project_the_hint_is_todays(self) -> None:
        for detector in (None, FixedDetector(None)):
            pipeline = self.pipeline(detector)
            for window, profile in self.TITLES:
                with self.subTest(detector=detector, title=window.title):
                    processed = self.run_on(pipeline, window)
                    self.assertEqual(processed.profile, profile)
                    expected = project_hint(window, ["Orion"]) if profile != "default" else ""
                    self.assertEqual((processed.project, processed.project_folder), (expected, None))

    def test_a_detected_project_names_its_folder(self) -> None:
        folder = Path("C:\\Work\\alpha-app")
        detector = FixedDetector(P.Project("alpha-app", folder, P.VSCODE_TITLE))
        pipeline = self.pipeline(detector)
        processed = self.run_on(pipeline, self.TITLES[0][0])
        self.assertEqual((processed.project, processed.project_folder), ("alpha-app", folder))
        self.run_on(pipeline, self.TITLES[1][0])
        processed = self.run_on(pipeline, self.TITLES[3][0])  # not an editor or Claude Code: never asked
        self.assertEqual((processed.project, processed.project_folder), ("", None))
        self.assertEqual([(pid, claude) for _, pid, claude in detector.calls], [(WT, True), (WT, False)])
        self.assertNotIn("alpha", repr(processed))


if __name__ == "__main__":
    unittest.main()
