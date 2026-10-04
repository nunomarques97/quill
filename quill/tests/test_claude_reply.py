"""The last Claude Code reply of a project folder: located and read read-only and bounded.

Everything runs on temporary folders standing in for the Claude Code config
folder and the pointers folder (``quill.tests`` forbids the real ones), with
invented messages. Links and reparse points are injected through ``lstat``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quill import claude_reply as R
from quill import notify as N
from quill.tests import real_claude_config_dir

FOLDER = "C:\\Invented\\zorblat-kit"
SESSION = "0f3c2a51-invented-session"
SECRET_WORD = "brumaflex"  # an invented word that must never reach a log line or a repr


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def tool_use() -> dict:
    return {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "C:\\Invented\\a.py"}}


def thinking(value: str = "pensamento escondido inventado") -> dict:
    return {"type": "thinking", "thinking": value, "signature": "x"}


def assistant(*blocks: dict, sidechain: bool = False, **extra: object) -> dict:
    return {"type": "assistant", "isSidechain": sidechain, "sessionId": SESSION,
            "message": {"content": list(blocks), "role": "assistant"}, **extra}


def user(content: object, sidechain: bool = False, **extra: object) -> dict:
    return {"type": "user", "isSidechain": sidechain, "sessionId": SESSION,
            "message": {"content": content, "role": "user"}, **extra}


def tool_results() -> dict:
    return user([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "resultado inventado"}])


def lines(*entries: object) -> bytes:
    return b"".join((entry if isinstance(entry, bytes) else json.dumps(entry).encode("utf-8")) + b"\n"
                    for entry in entries)


TURN = (
    user("Pergunta inventada anterior"),
    assistant(text("Resposta antiga que não conta")),
    user("Muda o ficheiro zorblat_config.toml, por favor"),
    assistant(thinking(), text("Vou ler o ficheiro.")),
    assistant(tool_use()),
    tool_results(),
    {"type": "attachment", "isSidechain": False, "attachment": {"type": "invented"}},
    assistant(text("Feito. Queres a opção 1 ou a opção 2?")),
    {"type": "last-prompt", "lastPrompt": "texto inventado"},
)
REPLY = "Vou ler o ficheiro.\n\nFeito. Queres a opção 1 ou a opção 2?"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.config = self.base / "claude-config"
        self.projects = self.config / "projects"
        self.sessions = self.projects / R.encode_folder(FOLDER)
        self.sessions.mkdir(parents=True)
        self.pointers = self.base / "pointers"
        self.now = time.time()

    def session(self, name: str, data: bytes, age_s: float = 0.0, folder: Path | None = None) -> Path:
        path = (self.sessions if folder is None else folder) / name
        path.write_bytes(data)
        os.utime(path, (self.now - age_s, self.now - age_s))
        return path

    def point(self, path: object, folder: str = FOLDER, age_s: float = 0.0, session: object = SESSION) -> Path:
        self.pointers.mkdir(exist_ok=True)
        record = {"folder": folder, "session_id": session, "transcript_path": str(path), "time": self.now - age_s}
        target = self.pointers / R.pointer_name(R.folder_identity(FOLDER))
        target.write_text(json.dumps(record), "ascii")
        return target

    def lookup(self, folder: object = FOLDER, **kwargs: object) -> R.ReplyLookup:
        kwargs.setdefault("config_dir", self.config)
        kwargs.setdefault("pointers_dir", self.pointers)
        kwargs.setdefault("wall", lambda: self.now)
        kwargs.setdefault("clock", lambda: 0.0)  # a fixed clock: a loaded machine never times the lookup out
        return R.last_reply(folder, **kwargs)


class PathRuleTest(unittest.TestCase):
    def test_config_folder_comes_from_the_environment_or_home(self) -> None:
        self.assertEqual(real_claude_config_dir({"CLAUDE_CONFIG_DIR": "X:\\cfg"}), Path("X:\\cfg"))
        self.assertEqual(real_claude_config_dir({}), Path.home() / ".claude")

    def test_the_real_folders_are_forbidden_in_tests(self) -> None:
        with self.assertRaises(AssertionError):
            R.last_reply(FOLDER)
        with self.assertRaises(AssertionError):
            R.last_reply(FOLDER, config_dir="X:\\Invented")

    def test_folders_are_encoded_as_claude_code_does(self) -> None:
        self.assertEqual(R.encode_folder("C:\\Invented\\zorblat-kit"), "C--Invented-zorblat-kit")
        self.assertEqual(R.encode_folder("D:\\a b\\ção_x.y"), "D--a-b---o-x-y")

    def test_folder_identity_is_case_insensitive_and_normalised(self) -> None:
        same = ("C:\\Invented\\Zorblat-Kit", "c:/invented/zorblat-kit/", "C:\\Invented\\.\\zorblat-kit\\")
        self.assertEqual({R.folder_identity(value) for value in same}, {R.folder_identity(FOLDER)})
        for bad in ("", None, 3, "relative\\x", "\\\\server\\share\\x", "\\\\?\\C:\\x", "\\\\.\\pipe\\x",
                    "//server/share", "C:\\", "C:", "C:x", "C:\\a\\..\\b", "C:\\a\\x\0", "C:\\" + "a" * R.MAX_PATH):
            with self.subTest(bad=repr(bad)[:30]):
                self.assertIsNone(R.folder_identity(bad))
        self.assertRegex(R.pointer_name(R.folder_identity(FOLDER)), r"^pointer-[0-9a-f]{32}\.json$")


class NewestTest(Case):
    def test_the_reply_after_the_last_real_user_message(self) -> None:
        self.session("a.jsonl", lines(*TURN))
        found = self.lookup()
        self.assertEqual((found.reason, found.source, found.pointer), (R.OK, R.SOURCE_NEWEST, R.NO_POINTER))
        self.assertEqual(found.text, REPLY)
        self.assertEqual(found.chars, len(REPLY))
        self.assertTrue(found.found)

    def test_two_sessions_give_the_newest(self) -> None:
        self.session("older.jsonl", lines(user("x"), assistant(text("Resposta da sessão antiga"))), age_s=60)
        self.session("newer.jsonl", lines(user("x"), assistant(text("Resposta da sessão nova"))), age_s=5)
        self.session("notes.txt", b"not a session")
        self.assertEqual(self.lookup().text, "Resposta da sessão nova")

    def test_only_files_directly_in_the_folder_count(self) -> None:
        nested = self.sessions / "memory"
        nested.mkdir()
        self.session("deep.jsonl", lines(user("x"), assistant(text("fundo"))), folder=nested)
        self.assertEqual(self.lookup().reason, R.NO_FILE)

    def test_missing_folders_give_none(self) -> None:
        self.assertEqual(self.lookup().reason, R.NO_FILE)
        self.assertEqual(self.lookup("C:\\Invented\\other").reason, R.NO_SESSIONS)
        self.assertEqual(self.lookup(config_dir=self.base / "missing").reason, R.NO_PROJECTS)

    def test_bad_folders_and_config_give_none(self) -> None:
        for folder in ("relative", "\\\\server\\share\\x", "C:\\", "C:\\a\\..\\b", None):
            with self.subTest(folder=folder):
                self.assertEqual(self.lookup(folder).reason, R.BAD_FOLDER)
        for config in ("\\\\server\\share\\.claude", "\\\\?\\C:\\x", "relative\\.claude", "C:\\a\\..\\b"):
            with self.subTest(config=config):
                self.assertEqual(self.lookup(config_dir=config).reason, R.BAD_CONFIG)

    def test_an_old_file_gives_none(self) -> None:
        self.session("a.jsonl", lines(*TURN), age_s=3 * 3600)
        self.assertEqual(self.lookup(max_age_s=2 * 3600).reason, R.STALE)
        self.assertEqual(self.lookup(max_age_s=4 * 3600).reason, R.OK)

    def test_the_listing_is_capped(self) -> None:
        for index, name in enumerate("abcde"):
            self.session(f"{name}.jsonl", lines(user("x"), assistant(text(f"resposta {name}"))), age_s=50 - index)
        self.assertEqual(self.lookup().text, "resposta e")
        with mock.patch.object(R, "MAX_ENTRIES", 3):
            self.assertIn(self.lookup().text, ("resposta a", "resposta b", "resposta c"))


class PointerTest(Case):
    def test_a_pointer_beats_the_newest_file(self) -> None:
        pointed = self.session("attended.jsonl", lines(user("x"), assistant(text("Resposta da sessão atendida"))),
                               age_s=120)
        self.session("automated.jsonl", lines(user("x"), assistant(text("Resposta de uma sessão automática"))))
        self.point(pointed)
        found = self.lookup()
        self.assertEqual((found.reason, found.source, found.pointer), (R.OK, R.SOURCE_POINTER, R.POINTER_USED))
        self.assertEqual(found.text, "Resposta da sessão atendida")

    def test_a_pointer_may_name_another_folder_of_the_projects_directory(self) -> None:
        # A session started in a subfolder of the Git root lives in that subfolder's directory.
        other = self.projects / R.encode_folder(FOLDER + "\\src")
        other.mkdir()
        pointed = self.session("sub.jsonl", lines(user("x"), assistant(text("Resposta da subpasta"))), folder=other)
        self.point(pointed)
        self.assertEqual(self.lookup().text, "Resposta da subpasta")

    def test_a_stale_pointer_falls_back_to_the_newest(self) -> None:
        pointed = self.session("attended.jsonl", lines(user("x"), assistant(text("atendida"))), age_s=120)
        self.session("automated.jsonl", lines(user("x"), assistant(text("automática"))))
        self.point(pointed, age_s=3 * 3600)
        found = self.lookup(max_age_s=2 * 3600)
        self.assertEqual((found.source, found.pointer, found.text), (R.SOURCE_NEWEST, R.POINTER_STALE, "automática"))
        self.point(pointed, age_s=-2 * R.MAX_FUTURE_S)  # dated in the future
        self.assertEqual(self.lookup().pointer, R.POINTER_STALE)

    def test_a_bad_pointer_falls_back_to_the_newest(self) -> None:
        self.session("newest.jsonl", lines(user("x"), assistant(text("mais recente"))))
        target = self.point(self.sessions / "missing.jsonl")
        cases = {
            b"{broken": R.POINTER_INVALID,
            b"[]": R.POINTER_INVALID,
            b"x" * (R.MAX_POINTER_RECORD + 1): R.POINTER_INVALID,
            json.dumps({"folder": "C:\\Invented\\other", "session_id": SESSION,
                        "transcript_path": "x", "time": self.now}).encode(): R.POINTER_OTHER,
            json.dumps({"folder": FOLDER, "session_id": SESSION, "transcript_path": 3,
                        "time": self.now}).encode(): R.POINTER_INVALID,
            json.dumps({"folder": FOLDER, "session_id": SESSION, "transcript_path": "x",
                        "time": "now"}).encode(): R.POINTER_INVALID,
        }
        for data, pointer in cases.items():
            with self.subTest(data=data[:40]):
                target.write_bytes(data)
                found = self.lookup()
                self.assertEqual((found.source, found.pointer, found.text), (R.SOURCE_NEWEST, pointer, "mais recente"))
        self.point(self.sessions / "missing.jsonl")
        self.assertEqual(self.lookup().pointer, "pointer_" + R.NO_FILE)

    def test_pointer_paths_outside_the_projects_folder_are_refused(self) -> None:
        self.session("newest.jsonl", lines(user("x"), assistant(text("mais recente"))))
        outside = self.base / "elsewhere"
        outside.mkdir()
        stray = self.session("stray.jsonl", lines(user("x"), assistant(text("fora"))), folder=outside)
        cases = {
            str(stray): "pointer_" + R.OUTSIDE,
            str(self.config / "stray.jsonl"): "pointer_" + R.OUTSIDE,
            str(self.projects / "stray.jsonl"): "pointer_" + R.OUTSIDE,
            str(self.sessions / "deep" / "stray.jsonl"): "pointer_" + R.OUTSIDE,
            str(self.sessions / ".." / ".." / ".." / "elsewhere" / "stray.jsonl"): "pointer_" + R.BAD_PATH,
            "\\\\server\\share\\projects\\x\\stray.jsonl": "pointer_" + R.BAD_PATH,
            "\\\\?\\" + str(self.sessions / "newest.jsonl"): "pointer_" + R.BAD_PATH,
            "\\\\.\\" + str(self.sessions / "newest.jsonl"): "pointer_" + R.BAD_PATH,
            "relative\\stray.jsonl": "pointer_" + R.BAD_PATH,
            str(self.sessions / "newest.jsonl") + ":stream.jsonl": "pointer_" + R.BAD_PATH,
            str(self.sessions / "newest.txt"): "pointer_" + R.BAD_PATH,
        }
        for path, pointer in cases.items():
            with self.subTest(path=path[-40:]):
                self.point(path)
                found = self.lookup()
                self.assertEqual((found.source, found.pointer, found.text), (R.SOURCE_NEWEST, pointer, "mais recente"))

    def test_the_hook_pointer_is_found_from_the_git_root(self) -> None:
        root = self.base / "Zorblat-Kit"
        (root / ".git").mkdir(parents=True)
        (root / "src").mkdir()
        sessions = self.projects / R.encode_folder(str(root / "src"))
        sessions.mkdir()
        pointed = self.session(SESSION + ".jsonl", lines(user("x"), assistant(text("da sessão do hook"))),
                               folder=sessions)
        environ = {N.ATTENDED_VARIABLE: "1"}
        event = {"cwd": str(root / "src"), "session_id": SESSION, "transcript_path": str(pointed)}
        self.assertEqual(N.leave_pointer(event, environ, self.pointers, lambda: True), "written")
        found = self.lookup(str(root).upper())
        self.assertEqual((found.source, found.text), (R.SOURCE_POINTER, "da sessão do hook"))


class LinkTest(Case):
    """Symlinks, junctions and reparse points, injected through ``lstat``."""

    def lstat_marking(self, target: Path, link: bool = False):
        def lstat(path: str) -> os.stat_result:
            info = os.lstat(path)
            if os.path.normcase(path) != os.path.normcase(str(target)):
                return info
            mode = (info.st_mode & ~stat.S_IFMT(info.st_mode)) | stat.S_IFLNK if link else info.st_mode
            return SimpleNamespace(st_mode=mode, st_mtime=info.st_mtime, st_ino=info.st_ino, st_dev=info.st_dev,
                                   st_size=info.st_size, st_file_attributes=R.FILE_ATTRIBUTE_REPARSE_POINT)
        return lstat

    def test_links_and_reparse_points_are_refused(self) -> None:
        path = self.session("a.jsonl", lines(*TURN))
        for target in (path, self.sessions, self.projects):
            for link in (False, True):
                with self.subTest(target=target.name, link=link):
                    found = self.lookup(lstat=self.lstat_marking(target, link))
                    self.assertEqual(found.reason, R.LINKED)
                    self.assertIsNone(found.text)

    def test_a_linked_pointed_file_falls_back(self) -> None:
        pointed = self.session("attended.jsonl", lines(user("x"), assistant(text("atendida"))), age_s=60)
        self.session("newest.jsonl", lines(user("x"), assistant(text("mais recente"))))
        self.point(pointed)
        found = self.lookup(lstat=self.lstat_marking(pointed))
        self.assertEqual((found.source, found.pointer, found.text), (R.SOURCE_NEWEST, "pointer_linked", "mais recente"))

    def test_a_linked_pointer_file_is_ignored(self) -> None:
        self.session("newest.jsonl", lines(user("x"), assistant(text("mais recente"))))
        pointer = self.point(self.sessions / "newest.jsonl")
        self.assertEqual(self.lookup(lstat=self.lstat_marking(pointer)).pointer, R.POINTER_INVALID)

    def test_a_file_replaced_after_the_check_is_refused(self) -> None:
        path = self.session("a.jsonl", lines(*TURN))

        def lstat(value: str) -> os.stat_result:
            info = os.lstat(value)
            if os.path.normcase(value) != os.path.normcase(str(path)):
                return info
            return SimpleNamespace(st_mode=info.st_mode, st_mtime=info.st_mtime, st_ino=info.st_ino + 1,
                                   st_dev=info.st_dev, st_size=info.st_size, st_file_attributes=0)
        self.assertEqual(self.lookup(lstat=lstat).reason, R.CHANGED)

    def test_a_folder_named_like_a_session_is_not_read(self) -> None:
        (self.sessions / "a.jsonl").mkdir()
        self.assertEqual(self.lookup().reason, R.NOT_REGULAR)

    def test_a_resolved_path_outside_the_projects_folder_is_refused(self) -> None:
        path = self.session("a.jsonl", lines(*TURN))
        with mock.patch.object(R.os.path, "realpath",
                               side_effect=lambda value: str(self.base / "elsewhere" / "a.jsonl")
                               if os.path.normcase(value) == os.path.normcase(str(path)) else value):
            self.assertEqual(self.lookup().reason, R.OUTSIDE)


class ParseTest(Case):
    def reply(self, *entries: object, max_chars: int = 1000) -> tuple[str | None, str]:
        return R.reply_text(lines(*entries), False, max_chars)

    def test_sidechain_entries_are_ignored(self) -> None:
        self.assertEqual(self.reply(user("x"), assistant(text("principal")),
                                    user("subtarefa", sidechain=True),
                                    assistant(text("da subtarefa"), sidechain=True)),
                         ("principal", R.OK))

    def test_tool_results_and_meta_entries_do_not_end_the_turn(self) -> None:
        self.assertEqual(self.reply(user("x"), assistant(text("antes")), tool_results(),
                                    user("caveat inventado", isMeta=True), assistant(text("depois"))),
                         ("antes\n\ndepois", R.OK))

    def test_a_tool_only_turn_gives_none(self) -> None:
        self.assertEqual(self.reply(user("x"), assistant(text("velha")), user("y"), assistant(thinking()),
                                    assistant(tool_use()), tool_results()), (None, R.NO_TEXT))
        self.assertEqual(self.reply(user("x")), (None, R.NO_TEXT))
        self.assertEqual(self.reply(), (None, R.NO_TEXT))

    def test_a_user_message_with_blocks_ends_the_turn(self) -> None:
        typed = user([{"type": "text", "text": "y"}, {"type": "tool_result", "content": "z"}])
        self.assertEqual(self.reply(assistant(text("velha")), typed, assistant(text("nova"))), ("nova", R.OK))
        self.assertEqual(self.reply(assistant(text("velha")), user([]), assistant(text("nova"))),
                         ("velha\n\nnova", R.OK))

    def test_api_errors_and_other_entries_are_ignored(self) -> None:
        self.assertEqual(self.reply(user("x"), assistant(text("certa")),
                                    assistant(text("API Error inventado"), isApiErrorMessage=True),
                                    {"type": "summary", "summary": "resumo"},
                                    {"message": "not a message", "type": "assistant"}),
                         ("certa", R.OK))

    def test_malformed_and_oversized_lines_are_skipped(self) -> None:
        big = assistant(text("z" * 300))
        data = lines(user("x"), assistant(text("boa")), b"{broken", b"\xff\xfe", b"[1, 2]", b"null", b"[" * 5000, big)
        with mock.patch.object(R, "MAX_LINE_BYTES", 200):
            self.assertEqual(R.reply_text(data, False, 1000), ("boa", R.OK))
        self.assertEqual(R.reply_text(data, False, 1000), ("boa\n\n" + "z" * 300, R.OK))

    def test_the_first_partial_line_of_a_tail_is_dropped(self) -> None:
        data = lines(assistant(text("cortada")), assistant(text("fim")))
        self.assertEqual(R.reply_text(data, True, 1000), ("fim", R.OK))
        self.assertEqual(R.reply_text(data, False, 1000), ("cortada\n\nfim", R.OK))

    def test_only_the_tail_is_read(self) -> None:
        early = lines(user("x"), assistant(text("início " * 200)))
        self.session("a.jsonl", early + lines(user("y"), assistant(text("fim"))))
        with mock.patch.object(R, "MAX_TAIL_BYTES", 400):
            read: list[int] = []
            real_open = open

            class Counting(io.FileIO):
                def read(self, size: int = -1) -> bytes:
                    data = super().read(size)
                    read.append(len(data))
                    return data
            with mock.patch("builtins.open", side_effect=lambda path, mode="r", *args, **kwargs:
                            Counting(path, "rb") if str(path).endswith(".jsonl") else real_open(path, mode, *args,
                                                                                                **kwargs)):
                self.assertEqual(self.lookup().text, "fim")
        self.assertLessEqual(sum(read), 400)

    def test_the_text_keeps_its_end(self) -> None:
        reply, reason = self.reply(user("x"), assistant(text("a" * 50)), assistant(text("b" * 50)), max_chars=60)
        self.assertEqual((reply, reason), ("a" * 8 + "\n\n" + "b" * 50, R.OK))
        self.session("a.jsonl", lines(user("x"), assistant(text("c" * 30 + "fim"))))
        self.assertEqual(self.lookup(max_chars=10).text, "c" * 7 + "fim")

    def test_control_characters_are_removed(self) -> None:
        self.assertEqual(self.reply(user("x"), assistant(text("a\x00b\x1b[31mc\td\re"))), ("ab[31mc\tde", R.OK))

    def test_a_slow_lookup_stops(self) -> None:
        self.session("a.jsonl", lines(*TURN))
        ticks = iter(range(0, 10_000))
        found = self.lookup(clock=lambda: next(ticks) * 0.1, max_time_s=0.25)
        self.assertEqual(found.reason, R.TIMEOUT)
        self.assertIsNone(found.text)


class PrivacyTest(Case):
    def test_text_never_appears_in_repr_or_logs(self) -> None:
        self.session(SESSION + ".jsonl", lines(user("x"), assistant(text(f"Usa o {SECRET_WORD} agora"))))
        self.point(self.sessions / (SESSION + ".jsonl"))
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        logger = logging.getLogger("quill")
        logger.addHandler(handler)
        old = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            found = self.lookup()
            self.lookup(lstat=lambda path: (_ for _ in ()).throw(RuntimeError(SECRET_WORD)))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)
        self.assertIn(SECRET_WORD, found.text)
        for shown in (repr(found), str(found), capture.getvalue()):
            self.assertNotIn(SECRET_WORD, shown)
            self.assertNotIn(SESSION, shown)
            self.assertNotIn("zorblat", shown.casefold())
            self.assertNotIn(str(self.base).casefold(), shown.casefold())
        self.assertIn("last reply: ok", capture.getvalue())

    def test_nothing_is_written_in_the_claude_config_folder(self) -> None:
        self.session("a.jsonl", lines(*TURN))
        pointed = self.session("b.jsonl", lines(user("x"), assistant(text("b"))), age_s=30)
        self.point(pointed)

        def snapshot() -> list[tuple[str, int, int]]:
            return sorted((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in self.config.rglob("*"))
        before = snapshot()
        self.lookup()
        self.lookup("C:\\Invented\\other")
        self.assertEqual(snapshot(), before)

    def test_a_failure_gives_none(self) -> None:
        found = self.lookup(lstat=lambda path: (_ for _ in ()).throw(RuntimeError("fake")))
        self.assertEqual((found.reason, found.text), (R.FAILED, None))


OFFER = ("Encontrei dois caminhos no brumaflex_loader.py.\n\n"
         "1. Reescrever o Travolino agora\n2. Deixar o Mirquelo para depois\n\nQual preferes?")


class Clock:
    def __init__(self, step: float = 0.012) -> None:
        self.now, self.step = 100.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class ReplyContextTest(Case):
    """``reply_context``: the derived context of a mouse 5 hold with the ``[claude_code]`` caps."""

    def settings(self, **changes: object) -> SimpleNamespace:
        values = dict(last_reply_context=True, last_reply_max_chars=4000, last_reply_max_terms=40,
                      last_reply_max_age_h=12)
        values.update(changes)
        return SimpleNamespace(**values)

    def reader(self, text: str | None = OFFER, reason: str = R.OK, source: str | None = R.SOURCE_POINTER):
        calls = []

        def read(folder: object, **kwargs: object) -> R.ReplyLookup:
            calls.append((folder, kwargs))
            return R.ReplyLookup(reason, source, text=text)

        return read, calls

    def test_a_reply_gives_its_terms_options_and_a_log_line_of_counts(self) -> None:
        read, calls = self.reader()
        found = R.reply_context(FOLDER, self.settings(), reader=read, clock=Clock())
        self.assertEqual(calls, [(FOLDER, {"max_chars": 4000, "max_age_s": 12 * 3600.0})])
        self.assertEqual((found.reason, found.source, found.options, found.ms), (R.OK, R.SOURCE_POINTER, 2, 12))
        self.assertTrue(found.context.asks)
        self.assertEqual(found.terms, len(found.context.terms))
        self.assertIn("Travolino", found.context.terms)
        self.assertEqual(found.log_fields(),
                         f"last reply ok (source pointer, {found.terms} terms, 2 options, 12 ms)")
        for shown in (found.log_fields(), repr(found), str(found)):
            self.assertNotIn("brumaflex", shown.casefold())
            self.assertNotIn("travolino", shown.casefold())
            self.assertNotIn("invented", shown.casefold())

    def test_the_caps_come_from_the_settings(self) -> None:
        read, calls = self.reader()
        found = R.reply_context(FOLDER, self.settings(last_reply_max_chars=200, last_reply_max_terms=2,
                                                      last_reply_max_age_h=1), reader=read)
        self.assertEqual(calls[0][1], {"max_chars": 200, "max_age_s": 3600.0})
        self.assertEqual(found.terms, 2)
        self.assertEqual(len(found.context.terms), 2)

    def test_no_reply_or_no_terms_gives_none_with_its_reason(self) -> None:
        cases = {
            "no session file": (dict(text=None, reason=R.NO_FILE, source=None), R.NO_FILE, None),
            "stale": (dict(text=None, reason=R.STALE, source=R.SOURCE_NEWEST), R.STALE, R.SOURCE_NEWEST),
            "a reply without terms": (dict(text="Ok."), R.NO_TERMS, R.SOURCE_POINTER),
        }
        for label, (kwargs, reason, source) in cases.items():
            with self.subTest(label):
                read, _ = self.reader(**kwargs)
                found = R.reply_context(FOLDER, self.settings(), reader=read)
                self.assertEqual((found.reason, found.source, found.context, found.terms, found.options),
                                 (reason, source, None, 0, 0))

    def test_a_failing_reader_gives_none_and_never_raises(self) -> None:
        def broken(folder: object, **kwargs: object) -> R.ReplyLookup:
            raise OSError("fake")

        found = R.reply_context(FOLDER, self.settings(), reader=broken)
        self.assertEqual((found.reason, found.context), (R.FAILED, None))
        found = R.reply_context(FOLDER, object(), reader=self.reader()[0])  # settings without the caps
        self.assertEqual((found.reason, found.context), (R.FAILED, None))

    def test_the_real_reader_on_a_temporary_config(self) -> None:
        self.session("a.jsonl", lines(user("Pergunta inventada"), assistant(text(OFFER))))

        def read(folder: object, **kwargs: object) -> R.ReplyLookup:
            return R.last_reply(folder, config_dir=self.config, pointers_dir=self.pointers,
                                wall=lambda: self.now, clock=lambda: 0.0, **kwargs)

        found = R.reply_context(FOLDER, self.settings(), reader=read)
        self.assertEqual((found.reason, found.source, found.options), (R.OK, R.SOURCE_NEWEST, 2))
        found = R.reply_context("C:\\Invented\\other", self.settings(), reader=read)
        self.assertEqual((found.reason, found.context), (R.NO_SESSIONS, None))


class ProjectsStatusTest(Case):
    def test_a_readable_projects_folder_gives_its_directory_count(self) -> None:
        (self.projects / "C--Invented-second").mkdir()
        (self.projects / "loose.txt").write_text("x", "ascii")
        self.assertEqual(R.projects_status(self.config), (R.OK, 2))

    def test_a_missing_linked_or_bad_folder_is_not_readable(self) -> None:
        self.assertEqual(R.projects_status(self.base / "missing"), (R.NO_PROJECTS, 0))
        self.assertEqual(R.projects_status("relative\\claude"), (R.BAD_CONFIG, 0))
        self.assertEqual(R.projects_status("\\\\server\\share\\claude"), (R.BAD_CONFIG, 0))

        def linked(path: str) -> os.stat_result:
            info = os.lstat(path)
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0, st_mtime=info.st_mtime)

        self.assertEqual(R.projects_status(self.config, lstat=linked), (R.LINKED, 0))

    def test_the_default_is_the_users_folder_which_tests_forbid(self) -> None:
        with self.assertRaises(AssertionError):
            R.projects_status()


class FakeFolders:
    def __init__(self, shortcuts: tuple = ()) -> None:
        self.shortcuts = shortcuts

    def shortcut_folders(self) -> tuple:
        return self.shortcuts


class DryRunTest(Case):
    """``python -m quill.claude_reply --dry-run``: reason codes and counts per known folder, never text or paths."""

    def settings_with(self, folders: tuple = (), on: bool = True) -> object:
        import dataclasses

        from quill.config import EXAMPLE_CONFIG, load_config

        config = load_config(None, EXAMPLE_CONFIG)
        return dataclasses.replace(
            config, project_context=dataclasses.replace(config.project_context, folders=folders),
            claude_code=dataclasses.replace(config.claude_code, last_reply_context=on))

    def reader(self, seen: list):
        def read(folder: object, **kwargs: object) -> R.ReplyLookup:
            seen.append(folder)
            if str(folder).casefold() == FOLDER.casefold():
                return R.ReplyLookup(R.OK, R.SOURCE_POINTER, text=OFFER)
            return R.ReplyLookup(R.NO_SESSIONS)

        return read

    def test_each_known_folder_once_with_counts_only(self) -> None:
        seen, out = [], []
        config = self.settings_with((("zorblat-kit", Path(FOLDER)), ("Zorblat Twin", Path(FOLDER.upper()))))
        shortcuts = FakeFolders((("Quarnel Hub", "C:\\Invented\\quarnel-hub"), ("dup", FOLDER),
                                 ("network", "\\\\server\\share\\x")))
        code = R.dry_run(config, out=out.append, reader=self.reader(seen), folders=shortcuts)
        self.assertEqual(code, 0)
        self.assertEqual([str(folder).casefold() for folder in seen],
                         [FOLDER.casefold(), "c:\\invented\\quarnel-hub"])  # once each, no UNC path
        self.assertEqual(out[0], "last reply context: on in the settings; 2 known project folders")
        self.assertRegex(out[1], r"^folder 1: ok \(source pointer, \d+ terms, 2 options, \d+ ms\)$")
        self.assertRegex(out[2], r"^folder 2: no_session_dir \(source none, 0 terms, 0 options, \d+ ms\)$")
        self.assertEqual(out[3], "1 of 2 folders have a reply context")
        printed = "\n".join(out).casefold()
        for private in ("zorblat", "quarnel", "invented", "brumaflex", "travolino", "server", "\\"):
            self.assertNotIn(private, printed)

    def test_off_in_the_settings_is_said_and_no_folder_gives_no_line(self) -> None:
        out = []
        R.dry_run(self.settings_with(on=False), out=out.append, reader=self.reader([]), folders=FakeFolders())
        self.assertEqual(out, ["last reply context: off in the settings; 0 known project folders",
                               "0 of 0 folders have a reply context"])

    def test_main_needs_the_dry_run_flag_and_reads_the_given_settings(self) -> None:
        settings = self.base / "quill.toml"
        settings.write_text("[project_context.folders]\n\"zorblat-kit\" = 'C:\\Invented\\zorblat-kit'\n", "utf-8")
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            R.main(["--config", str(settings)])
        seen, out = [], []
        self.assertEqual(R.main(["--dry-run", "--config", str(settings)], out=out.append, reader=self.reader(seen),
                                folders=FakeFolders()), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(out[-1], "1 of 1 folders have a reply context")
        settings.write_text("[claude_code]\nlast_reply_max_terms = 0\n", "utf-8")
        out = []
        self.assertEqual(R.main(["--dry-run", "--config", str(settings)], out=out.append, reader=self.reader([]),
                                folders=FakeFolders()), 2)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
