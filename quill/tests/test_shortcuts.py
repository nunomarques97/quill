"""quill.shortcuts tests: shell links built in memory, invented names, a fake launcher.

The .lnk files are synthetic (built by ``link_bytes`` in temporary folders);
nothing is resolved through the shell and nothing is opened.
"""

from __future__ import annotations

import logging
import struct
import tempfile
import unittest
from pathlib import Path

from quill import shortcuts as SC
from quill.shortcuts import LinkError, LinkTarget, Shortcut, list_shortcuts, match, parse_link
from quill.vocabulary import Entry, Vocabulary

CODE = "C:\\Tools\\Microsoft VS Code\\Code.exe"
FOLDER = "C:\\Work\\nimbus-deck"


def _ansi(text: str) -> bytes:
    return text.encode("ascii") + b"\0"


def _wide(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\0\0"


def _link_info(path: str, unicode_info: bool, local: bool = True) -> bytes:
    header = 0x24 if unicode_info else 0x1C
    volume = struct.pack("<IIII", 0x11, 3, 0x1234, 0x10) + b"\0"
    base, suffix = _ansi(path), _ansi("")
    offsets_at = header
    volume_at = offsets_at
    base_at = volume_at + len(volume)
    suffix_at = base_at + len(base)
    tail = volume + base + suffix
    extra = b""
    if unicode_info:
        wide_base_at = suffix_at + len(suffix)
        wide_suffix_at = wide_base_at + len(_wide(path))
        tail += _wide(path) + _wide("")
        extra = struct.pack("<II", wide_base_at, wide_suffix_at)
    flags = 0x1 if local else 0x2
    size = header + len(tail)
    return struct.pack("<IIIIIII", size, header, flags, volume_at, base_at, 0, suffix_at) + extra + tail


def link_bytes(target: str = CODE, *, arguments: str = "", directory: bool = False, unicode_info: bool = True,
               id_list: bool = True, link_info: bool = True, local: bool = True, env_target: str | None = None,
               name: str | None = None) -> bytes:
    """A minimal MS-SHLLINK file pointing to ``target``."""
    flags = SC.IS_UNICODE
    body = b""
    if id_list:
        flags |= SC.HAS_ID_LIST
        body += struct.pack("<H", 2) + b"\0\0"
    if link_info:
        flags |= SC.HAS_LINK_INFO
        body += _link_info(target, unicode_info, local)
    for flag, text in ((SC.HAS_NAME, name), (SC.HAS_ARGUMENTS, arguments or None)):
        if text is not None:
            flags |= flag
            body += struct.pack("<H", len(text)) + text.encode("utf-16-le")
    if env_target is not None:
        flags |= SC.HAS_EXP_STRING
        ansi = env_target.encode("ascii").ljust(260, b"\0")
        wide = env_target.encode("utf-16-le").ljust(520, b"\0")
        body += struct.pack("<II", SC.ENVIRONMENT_BLOCK_SIZE, SC.ENVIRONMENT_BLOCK) + ansi + wide
    body += b"\0\0\0\0"
    attributes = SC.FILE_ATTRIBUTE_DIRECTORY if directory else 0x20
    header = struct.pack("<I", SC.HEADER_SIZE) + SC.LINK_CLSID + struct.pack("<II", flags, attributes)
    return header + b"\0" * (SC.HEADER_SIZE - len(header)) + body


def write_link(folder: Path, name: str, target: str = CODE, **options: object) -> Path:
    path = folder / f"{name}.lnk"
    path.write_bytes(link_bytes(target, **options))
    return path


class FakeLauncher:
    def __init__(self, error: Exception | None = None) -> None:
        self.opened: list[Path] = []
        self.started: list[list[str]] = []
        self.error = error

    def open_shortcut(self, path: Path) -> None:
        if self.error is not None:
            raise self.error
        self.opened.append(path)

    def start(self, argv: list[str]) -> None:
        if self.error is not None:
            raise self.error
        self.started.append(list(argv))


def shortcut(name: str, folder: int = 0) -> Shortcut:
    return Shortcut(name, Path(f"hub{folder}") / f"{name}.lnk", folder)


def reader_for(targets: dict[Path, LinkTarget | None]):
    return lambda path: targets.get(path)


class FolderCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def folder(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        return path


# ---------------------------------------------------------------- reading


class ParseLinkTest(unittest.TestCase):
    def test_unicode_link_info_path_and_arguments(self) -> None:
        target = parse_link(link_bytes(CODE, arguments='--new-window "C:\\Work\\alfa.code-workspace"'))
        self.assertEqual(target.path, CODE)
        self.assertEqual(target.arguments, '--new-window "C:\\Work\\alfa.code-workspace"')
        self.assertFalse(target.directory)

    def test_ansi_link_info_without_id_list(self) -> None:
        target = parse_link(link_bytes(FOLDER, directory=True, unicode_info=False, id_list=False, name="x"))
        self.assertEqual((target.path, target.arguments, target.directory), (FOLDER, "", True))

    def test_environment_block_when_there_is_no_link_info(self) -> None:
        target = parse_link(link_bytes("", link_info=False, env_target="%QUILL_TEST_ROOT%\\Code.exe"))
        self.assertTrue(target.path.endswith("Code.exe"))

    def test_invalid_bytes_are_refused(self) -> None:
        good = link_bytes(CODE)
        for data in (b"", b"\x4c\0\0\0" + b"\0" * 80, good[:90], good[:SC.HEADER_SIZE + 10],
                     link_bytes(CODE, local=False), link_bytes("", link_info=False)):
            with self.subTest(size=len(data)), self.assertRaises(LinkError):
                parse_link(data)

    def test_link_info_offsets_out_of_range_are_refused(self) -> None:
        data = bytearray(link_bytes(CODE))
        info_at = SC.HEADER_SIZE + 4
        struct.pack_into("<I", data, info_at + 28, 0xFFFF)  # the unicode base path offset
        with self.assertRaises(LinkError):
            parse_link(bytes(data))

    def test_identity_ignores_case_and_separators(self) -> None:
        self.assertEqual(LinkTarget("C:\\Work\\Alfa\\", "x").identity, LinkTarget("c:/work/alfa", "x ").identity)
        self.assertNotEqual(LinkTarget(CODE, "a").identity, LinkTarget(CODE, "b").identity)


class ReadLinkTest(FolderCase):
    def test_reads_a_file_and_refuses_a_large_one(self) -> None:
        folder = self.folder("hub")
        self.assertEqual(SC.read_link(write_link(folder, "alfa")).path, CODE)
        big = folder / "big.lnk"
        big.write_bytes(link_bytes(CODE) + b"\0" * SC.MAX_LINK_BYTES)
        with self.assertLogs("quill.shortcuts", "WARNING") as logs:
            self.assertIsNone(SC.read_link(big))
            self.assertIsNone(SC.read_link(folder / "missing.lnk"))
            junk = folder / "junk.lnk"
            junk.write_bytes(b"not a link")
            self.assertIsNone(SC.read_link(junk))
        self.assertFalse(any("hub" in line or "junk" in line or "big" in line for line in logs.output))


# ---------------------------------------------------------------- listing


class ListShortcutsTest(FolderCase):
    def test_lists_only_lnk_files_directly_inside_each_folder(self) -> None:
        first, second = self.folder("one"), self.folder("two")
        write_link(first, "tarvo-kit")
        write_link(first, "alfa")
        (first / "notes.txt").write_text("x", "utf-8")
        (first / "folder.lnk").mkdir()  # a folder named like a shortcut does not count
        nested = first / "nested"
        nested.mkdir()
        write_link(nested, "hidden-deeper")
        write_link(second, "nimbus-deck")
        listing = list_shortcuts([first, second])
        self.assertEqual([(s.name, s.folder) for s in listing.shortcuts],
                         [("alfa", 0), ("tarvo-kit", 0), ("nimbus-deck", 1)])
        self.assertEqual((listing.folders_read, listing.folders_failed, listing.truncated), (2, 0, False))

    def test_missing_or_unreadable_folders_are_logged_by_reason_and_skipped(self) -> None:
        good = self.folder("good")
        write_link(good, "alfa")
        not_folder = self.root / "plain.txt"
        not_folder.write_text("x", "utf-8")
        with self.assertLogs("quill.shortcuts", "WARNING") as logs:
            listing = list_shortcuts([self.root / "secret-missing", not_folder, good])
        self.assertEqual([s.name for s in listing.shortcuts], ["alfa"])
        self.assertEqual((listing.folders_read, listing.folders_failed), (1, 2))
        joined = "\n".join(logs.output)
        self.assertIn("shortcut folder 0: missing", joined)
        self.assertIn("shortcut folder 1:", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn(str(self.root), joined)

    def test_the_number_of_shortcuts_is_bounded(self) -> None:
        folder = self.folder("many")
        for index in range(6):
            write_link(folder, f"proj-{index}")
        with self.assertLogs("quill.shortcuts", "WARNING"):
            listing = list_shortcuts([folder, folder], limit=4)
        self.assertEqual(len(listing.shortcuts), 4)
        self.assertTrue(listing.truncated)


# ---------------------------------------------------------------- matching


SIBLINGS = [shortcut("nimbus-deck"), shortcut("nimbus-deck-public"), shortcut("tarvo-kit"), shortcut("orla"),
            shortcut("orla-public"), shortcut("velinor-app")]
SAME = LinkTarget(CODE, "--new-window a")


class MatchTest(unittest.TestCase):
    def matched(self, spoken: str, items=SIBLINGS, vocabulary=Vocabulary(), reader=None) -> SC.Match:
        return match(spoken, items, vocabulary, reader=reader or (lambda path: SAME))

    def test_an_exact_name_beats_a_longer_sibling(self) -> None:
        for spoken, expected in (("nimbus deck", "nimbus-deck"), ("Nimbus Deck public", "nimbus-deck-public"),
                                 ("orla", "orla"), ("orla public", "orla-public"), ("ORLA-PUBLIC.", "orla-public"),
                                 ("tárvo kit", "tarvo-kit")):
            with self.subTest(spoken=spoken):
                found = self.matched(spoken)
                self.assertEqual((found.reason, found.shortcut.name, found.distance), (SC.MATCHED, expected, 0))

    def test_a_close_spelling_inside_the_bound_opens_when_clearly_ahead(self) -> None:
        found = self.matched("velinor ap")
        self.assertEqual((found.reason, found.shortcut.name, found.distance), (SC.MATCHED, "velinor-app", 1))
        found = self.matched("tarvokits")
        self.assertEqual(found.shortcut.name, "tarvo-kit")

    def test_short_names_need_an_exact_match(self) -> None:
        found = self.matched("orle")  # four letters: one edit allowed, but orla-public is not far behind? no
        self.assertEqual(found.shortcut.name, "orla")
        found = match("ori", [shortcut("orl")], reader=lambda path: SAME)
        self.assertEqual(found.reason, SC.NO_MATCH)
        self.assertEqual(found.options, ("orl",))

    def test_a_fuzzy_match_not_clearly_ahead_opens_nothing(self) -> None:
        items = [shortcut("velinor-app"), shortcut("velinor-apx")]
        found = match("velinor apz", items, reader=lambda path: SAME)
        self.assertEqual(found.reason, SC.AMBIGUOUS)
        self.assertIsNone(found.shortcut)
        self.assertEqual(found.options, ("velinor-app", "velinor-apx"))

    def test_no_match_shows_up_to_three_closest_names(self) -> None:
        found = self.matched("public")
        self.assertEqual(found.reason, SC.NO_MATCH)
        self.assertIsNone(found.shortcut)
        self.assertLessEqual(len(found.options), SC.MAX_OPTIONS)
        # Names that contain the spoken word come first.
        self.assertEqual(found.options[:2], ("orla-public", "nimbus-deck-public"))
        self.assertEqual(self.matched("zzzzzzzz").reason, SC.NO_MATCH)

    def test_a_name_part_is_an_option_never_a_match(self) -> None:
        found = self.matched("nimbus")
        self.assertEqual(found.reason, SC.NO_MATCH)
        self.assertEqual(found.options[:2], ("nimbus-deck", "nimbus-deck-public"))

    def test_no_shortcuts_and_empty_names(self) -> None:
        self.assertEqual(match("alfa", []).reason, SC.NO_SHORTCUTS)
        self.assertEqual(self.matched("...").reason, SC.NO_MATCH)

    def test_the_vocabulary_resolves_names_and_variants(self) -> None:
        vocabulary = Vocabulary(names=(Entry("Tarvo-Kit", "name", ("tarbo quite",)),
                                       Entry("Velinor-App", "name", ())))
        found = self.matched("tarbo quite", vocabulary=vocabulary)
        self.assertEqual((found.reason, found.shortcut.name), (SC.MATCHED, "tarvo-kit"))
        found = self.matched("velinnor app", vocabulary=vocabulary)
        self.assertEqual(found.shortcut.name, "velinor-app")
        # Without the vocabulary the variant is too far from any name.
        self.assertEqual(self.matched("tarbo quite").reason, SC.NO_MATCH)

    def test_the_vocabulary_never_hides_a_second_exact_candidate(self) -> None:
        # The spoken name is exactly one shortcut, and a variant of another name: ambiguous.
        vocabulary = Vocabulary(names=(Entry("Tarvo-Kit", "name", ("orla",)),))
        found = self.matched("orla", vocabulary=vocabulary)
        self.assertEqual(found.reason, SC.AMBIGUOUS)
        self.assertEqual(set(found.options[:2]), {"orla", "tarvo-kit"})

    def test_the_same_name_in_two_folders_with_the_same_target_is_one_candidate(self) -> None:
        items = [shortcut("orla", 0), shortcut("orla", 1), shortcut("tarvo-kit", 1)]
        found = match("orla", items, reader=lambda path: LinkTarget(CODE, "--new-window orla"))
        self.assertEqual((found.reason, found.shortcut, found.candidates), (SC.MATCHED, items[0], 2))

    def test_the_same_name_with_different_targets_is_ambiguous(self) -> None:
        items = [shortcut("orla", 0), shortcut("orla", 1)]
        targets = {items[0].path: LinkTarget(CODE, "--new-window orla"), items[1].path: LinkTarget(FOLDER)}
        found = match("orla", items, reader=reader_for(targets))
        self.assertEqual((found.reason, found.options), (SC.AMBIGUOUS, ("orla",)))
        # An unreadable twin cannot be proven the same: ambiguous too.
        found = match("orla", items, reader=reader_for({items[0].path: SAME}))
        self.assertEqual(found.reason, SC.AMBIGUOUS)

    def test_match_repr_carries_no_names(self) -> None:
        found = self.matched("nimbus deck")
        self.assertNotIn("nimbus", repr(found))
        self.assertNotIn("nimbus", repr(found.shortcut))


# ---------------------------------------------------------------- launching


class LaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.launcher = FakeLauncher()
        self.item = shortcut("nimbus-deck")

    def launched(self, target, *, vscode=lambda: Path(CODE), is_dir=lambda path: True, launcher=None):
        return SC.launch(self.item, launcher or self.launcher, reader=lambda path: target, vscode=vscode,
                         is_dir=is_dir)

    def test_a_vscode_shortcut_is_opened_with_the_shell(self) -> None:
        result = self.launched(LinkTarget(CODE, '--new-window "C:\\Work\\x.code-workspace"'))
        self.assertEqual((result.reason, result.how), (SC.OPENED, "shortcut"))
        self.assertEqual(self.launcher.opened, [self.item.path])
        self.assertEqual(self.launcher.started, [])

    def test_a_folder_shortcut_starts_vscode_with_an_argument_list(self) -> None:
        result = self.launched(LinkTarget(FOLDER, "", True))
        self.assertEqual((result.reason, result.how), (SC.OPENED, "folder"))
        self.assertEqual(self.launcher.started, [[CODE, "--new-window", FOLDER]])
        self.assertEqual(self.launcher.opened, [])

    def test_other_targets_are_refused(self) -> None:
        for target in (LinkTarget("C:\\Tools\\other.exe"), LinkTarget("C:\\Work\\notes.txt"),
                       LinkTarget("C:\\Tools\\cmd.exe", "/c whatever"), LinkTarget("code.exe"),
                       LinkTarget("C:\\Tools\\Code.exe.lnk")):
            with self.subTest(target=target.path):
                self.assertEqual(self.launched(target, is_dir=lambda path: False).reason, SC.REFUSED)
        # A network share is never started, even when it is a folder.
        self.assertEqual(self.launched(LinkTarget("\\\\server\\share\\x", "", True)).reason, SC.REFUSED)
        self.assertEqual(self.launcher.opened + self.launcher.started, [])

    def test_unreadable_missing_vscode_and_launch_failures(self) -> None:
        self.assertEqual(self.launched(None).reason, SC.UNREADABLE)
        self.assertEqual(self.launched(LinkTarget(FOLDER, "", True), vscode=lambda: None).reason, SC.VSCODE_MISSING)
        failing = FakeLauncher(OSError("fake failure"))
        with self.assertLogs("quill.shortcuts", "WARNING") as logs:
            self.assertEqual(self.launched(LinkTarget(CODE), launcher=failing).reason, SC.LAUNCH_FAILED)
        self.assertNotIn("nimbus", "\n".join(logs.output))
        self.assertEqual(self.launcher.opened + self.launcher.started, [])

    def test_find_vscode_uses_the_install_folders(self) -> None:
        env = {"LOCALAPPDATA": "C:\\Local", "ProgramFiles": "C:\\Programs"}
        expected = Path("C:\\Programs", "Microsoft VS Code", "Code.exe")
        self.assertEqual(SC.find_vscode(env, exists=lambda path: path == expected), expected)
        self.assertIsNone(SC.find_vscode(env, exists=lambda path: False))
        self.assertEqual(SC.vscode_candidates(env)[0], Path("C:\\Local", "Programs", "Microsoft VS Code", "Code.exe"))

    def test_the_real_launcher_never_uses_a_shell(self) -> None:
        import subprocess
        from unittest import mock

        # The package forbids building a real launcher; this one is built past that guard and Popen is a mock.
        with self.assertRaises(AssertionError):
            SC.ShellLauncher()
        launcher = object.__new__(SC.ShellLauncher)
        with mock.patch.object(subprocess, "Popen") as popen:
            launcher.start([CODE, "--new-window", FOLDER])
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [CODE, "--new-window", FOLDER])
        self.assertIs(kwargs["shell"], False)


if __name__ == "__main__":
    unittest.main()
