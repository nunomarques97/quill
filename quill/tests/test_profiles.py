"""quill.profiles tests with fake window info and invented texts.

No real window is read: a fake Win32 layer answers the few read-only calls
``window_info`` makes, and any other call fails.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quill import profiles, uia
from quill.cleanup import Cleanup, clean_text
from quill.config import ProfileMatcher, load_config
from quill.profiles import DEFAULT, Profiles, StyleError, WindowInfo, apply_profile, load_style_samples, style_prompt

TERMINAL = "WindowsTerminal.exe"


def window(process="", window_class="", title=""):
    return WindowInfo(process, window_class, title)


EXAMPLE = Profiles(load_config(None).profiles)


class FakeWindows:
    """Read-only window queries; ``images`` maps pid to a full image path (missing: unreadable)."""

    def __init__(self):
        self.windows = {10: 1, 20: 2, 30: 0}
        self.images = {1: "C:\\Program Files\\Invented\\Code.exe"}
        self.classes = {10: "Chrome_WidgetWin_1", 20: "CASCADIA_HOSTING_WINDOW_CLASS"}
        self.titles = {10: "invented-folder - Visual Studio Code [Claude Code]", 20: "Claude Code"}
        self.fail = set()

    def is_window(self, hwnd):
        if "is_window" in self.fail:
            raise OSError("gone")
        return hwnd in self.windows

    def window_process_id(self, hwnd):
        return self.windows[hwnd]

    def process_image(self, pid):
        if "process_image" in self.fail:
            raise OSError("access denied")
        return self.images.get(pid, "")

    def window_class(self, hwnd):
        return self.classes.get(hwnd, "")

    def window_text(self, hwnd):
        if "window_text" in self.fail:
            raise OSError("hung")
        return self.titles.get(hwnd, "")


class MatcherTest(unittest.TestCase):
    def test_example_profiles(self):
        self.assertEqual(EXAMPLE.select(window(TERMINAL, title="✳ Claude Code")), "claude-code")
        self.assertEqual(EXAMPLE.select(window("code.exe", title="main.py - invented - Visual Studio Code")), "vscode")
        self.assertEqual(EXAMPLE.select(window("WhatsApp.exe", title="WhatsApp")), "whatsapp")
        self.assertEqual(EXAMPLE.select(window("OUTLOOK.EXE", title="Inbox")), "email")
        self.assertEqual(EXAMPLE.select(window("olk.exe")), "email")

    def test_precedence_first_match_wins(self):
        # The Claude Code sidebar view in VS Code matches both claude-code and vscode: claude-code comes first.
        info = window("Code.exe", title="invented - Visual Studio Code [Claude Code]")
        self.assertEqual(EXAMPLE.select(info), "claude-code")
        self.assertTrue(EXAMPLE.is_claude_code(info))
        reordered = Profiles([ProfileMatcher("vscode", processes=("Code.exe",)),
                              ProfileMatcher("claude-code", processes=("Code.exe",), titles=("[Claude Code]",))])
        self.assertEqual(reordered.select(info), "vscode")
        self.assertFalse(reordered.is_claude_code(info))

    def test_vscode_is_claude_code_only_with_the_focused_view_marker(self):
        # window.title ends with " [${focusedView}]": only the focused Claude Code view gives the marker.
        for title in ("invented - Visual Studio Code [Claude Code]", "● x.py - invented - Visual Studio Code [claude  code]"):
            self.assertTrue(EXAMPLE.is_claude_code(window("Code.exe", "Chrome_WidgetWin_1", title)), title)
        for title in (
            "Claude Code - invented - Visual Studio Code",  # a file or folder named Claude Code, no marker
            "Invented topic - invented - Visual Studio Code []",  # the Claude Code editor tab: no focused view
            "Claude Code notes.md - invented - Visual Studio Code [Text Editor]",
            "invented - Visual Studio Code [Terminal]",  # the integrated terminal, whatever runs in it
            "invented - Visual Studio Code [Explorer]",
            "invented - Visual Studio Code",  # window.title not configured
        ):
            info = window("Code.exe", "Chrome_WidgetWin_1", title)
            self.assertEqual(EXAMPLE.select(info), "vscode", title)
            self.assertFalse(EXAMPLE.is_claude_code(info), title)
        # "[Claude Code]" or "Claude Code" inside a file or folder name is not the trailing focused-view marker.
        for title in (
            "[Claude Code].md - invented - Visual Studio Code",
            "[Claude Code].md - invented - Visual Studio Code [Text Editor]",
            "notes [Claude Code] draft.md - invented - Visual Studio Code [Explorer]",
            "invented [Claude Code] - Visual Studio Code",
            "invented - Visual Studio Code [Claude Code]x",
            "invented - Visual Studio Code[Claude Code]",  # no space before the marker
        ):
            info = window("Code.exe", "Chrome_WidgetWin_1", title)
            self.assertEqual(EXAMPLE.select(info), "vscode", title)
            self.assertFalse(EXAMPLE.is_claude_code(info), title)
        self.assertTrue(EXAMPLE.is_claude_code(window("Code.exe", title="[Claude Code].md - x - Visual Studio Code "
                                                                        "[Claude Code]")))
        # A plain title entry still matches anywhere in the title.
        self.assertTrue(profiles.matches(ProfileMatcher("vscode", titles=("[x",)), window("a.exe", title="a [x] b")))
        # The marker counts only in VS Code; Windows Terminal keeps its plain title rule.
        self.assertFalse(EXAMPLE.is_claude_code(window("chrome.exe", title="Page [Claude Code]")))
        self.assertTrue(EXAMPLE.is_claude_code(window(TERMINAL, title="Claude Code")))

    def test_vscode_marker_before_the_screen_reader_state_suffix(self):
        # editor.accessibilitySupport "on" with accessibility.windowTitleOptimized appends
        # " - ${activeEditorState}" after the focused-view marker.
        for title in (
            "main.py - invented - Visual Studio Code [Claude Code] - Modified",
            "● main.py - invented - Visual Studio Code [Claude Code] - Modified, 2 problems",
            "alpha-app | notes.md - Visual Studio Code [Claude Code] - Untracked",
            "alpha-app | notes.md - Visual Studio Code [Claude Code]",
            "alpha-app | - Visual Studio Code [Claude Code]",
            "[Claude Code].md - invented - Visual Studio Code [Claude Code] - Modified",
        ):
            info = window("Code.exe", "Chrome_WidgetWin_1", title)
            self.assertEqual(EXAMPLE.select(info), "claude-code", title)
        for title in (
            "main.py - invented - Visual Studio Code [Text Editor] - Modified",
            "main.py - invented - Visual Studio Code [Editor de Texto] - Modificado",
            "alpha-app | Invented topic - Visual Studio Code []",  # the editor-tab panel: no positive marker
            "alpha-app | Invented topic - Visual Studio Code [] - Modified",
            "alpha-app | notes.md - Visual Studio Code [Terminal] - Modified",
            "alpha-app | notes.md - Visual Studio Code [Explorer]",
            "alpha-app | notes.md - Visual Studio Code - Modified",  # no marker at all
            # The marker text inside a name before the app name, or after a state, never counts.
            "notes [Claude Code] - invented - Visual Studio Code [Text Editor] - Modified",
            "Visual Studio Code [Claude Code] - notes.md - invented - Visual Studio Code [Text Editor]",
            "alpha-app | x [Claude Code] - Visual Studio Code [] - Modified",
            "main.py - invented - Visual Studio Code [Text Editor] - x [Claude Code]",
            "main.py - invented - Visual Studio Code [Claude Code] -",
            "main.py - invented - Visual Studio Code [Claude Code] extra",
        ):
            info = window("Code.exe", "Chrome_WidgetWin_1", title)
            self.assertEqual(EXAMPLE.select(info), "vscode", title)
            self.assertFalse(EXAMPLE.is_claude_code(info), title)
        # Terminal matchers are unchanged: the plain substring rule, also with a suffix.
        self.assertTrue(EXAMPLE.is_claude_code(window(TERMINAL, title="✳ Claude Code - Modified")))
        self.assertFalse(EXAMPLE.is_claude_code(window(TERMINAL, title="Visual Studio Code [Claude] - x")))

    def test_split_vscode_title(self):
        split = profiles.split_vscode_title("● a.py - alpha - Visual  Studio Code [Claude Code] - Modified")
        self.assertEqual((split.head, split.marker, split.state), ("● a.py - alpha - ", "Claude Code", "Modified"))
        split = profiles.split_vscode_title("alpha | - Visual Studio Code []")
        self.assertEqual((split.marker, split.state), ("", None))
        split = profiles.split_vscode_title("alpha - Visual Studio Code")
        self.assertEqual((split.marker, split.state), (None, None))
        for title in ("", "Notepad", "alpha - Visual Studio Code Insiders", "alpha - Visual Studio Code [x",
                      "alpha - Visual Studio Code [x] y", "a" * 1025 + " - Visual Studio Code", None):
            self.assertIsNone(profiles.split_vscode_title(title), title)

    def test_claude_code_needs_the_process_and_the_title(self):
        self.assertEqual(EXAMPLE.select(window(TERMINAL, title="PowerShell")), DEFAULT)
        self.assertEqual(EXAMPLE.select(window("chrome.exe", title="Claude Code docs")), DEFAULT)
        self.assertFalse(EXAMPLE.is_claude_code(window(TERMINAL, title="PowerShell")))

    def test_every_listed_field_must_match(self):
        matcher = ProfileMatcher("whatsapp", processes=("Invented.exe",), classes=("ChatWindow",), titles=("chat",))
        self.assertTrue(profiles.matches(matcher, window("invented.EXE", "chatwindow", "Group CHAT - Invented")))
        self.assertFalse(profiles.matches(matcher, window("Invented.exe", "Other", "chat")))
        self.assertFalse(profiles.matches(matcher, window("Invented.exe", "ChatWindow", "mail")))
        # Processes and classes are whole names, not substrings.
        self.assertFalse(profiles.matches(matcher, window("Invented.exe.bak", "ChatWindow", "chat")))
        self.assertFalse(profiles.matches(ProfileMatcher("email"), window("x.exe")))

    def test_unknown_windows_get_the_default_profile(self):
        self.assertEqual(EXAMPLE.select(None), DEFAULT)
        self.assertEqual(EXAMPLE.select(window()), DEFAULT)
        self.assertEqual(EXAMPLE.select(window("notepad.exe", "Notepad", "Untitled")), DEFAULT)
        self.assertFalse(EXAMPLE.is_claude_code(None))

    def test_unreadable_process_never_matches_a_process_list(self):
        # An elevated terminal: the title reads, the process image does not.
        self.assertEqual(EXAMPLE.select(window("", title="Claude Code")), DEFAULT)
        self.assertFalse(EXAMPLE.is_claude_code(window("", title="Claude Code")))
        # A title-only matcher still applies: nothing about the process is guessed.
        titled = Profiles([ProfileMatcher("email", titles=("Invented Mail",))])
        self.assertEqual(titled.select(window("", title="Inbox - Invented Mail")), "email")

    def test_unknown_profile_name_is_rejected(self):
        with self.assertRaises(ValueError):
            Profiles([ProfileMatcher("slack", processes=("x.exe",))])


class WindowInfoTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeWindows()

    def test_reads_name_class_and_title(self):
        info = profiles.window_info(self.api, 10)
        self.assertEqual(info, WindowInfo("Code.exe", "Chrome_WidgetWin_1", "invented-folder - Visual Studio Code [Claude Code]"))
        self.assertEqual(EXAMPLE.select(info), "claude-code")

    def test_elevated_process_reads_as_unknown(self):
        info = profiles.window_info(self.api, 20)  # no readable image for pid 2
        self.assertEqual(info.process, "")
        self.assertEqual(info.title, "Claude Code")
        self.assertEqual(EXAMPLE.select(info), DEFAULT)
        self.api.fail.add("process_image")
        self.assertEqual(profiles.window_info(self.api, 10).process, "")

    def test_gone_or_missing_windows(self):
        self.assertIsNone(profiles.window_info(self.api, 0))
        self.assertIsNone(profiles.window_info(self.api, 99))
        self.assertEqual(profiles.window_info(self.api, 30), WindowInfo("", "", ""))  # no process id
        self.api.fail.add("is_window")
        self.assertIsNone(profiles.window_info(self.api, 10))

    def test_unreadable_title_is_empty(self):
        self.api.fail.add("window_text")
        info = profiles.window_info(self.api, 10)
        self.assertEqual((info.process, info.title), ("Code.exe", ""))
        self.assertEqual(EXAMPLE.select(info), "vscode")


class RulesTest(unittest.TestCase):
    def check(self, profile, text, expected, keep=()):
        shaped = apply_profile(text, profile, keep)
        self.assertEqual(shaped, expected)
        self.assertEqual(apply_profile(shaped, profile, keep), shaped, "applying twice changes the text")
        self.assertEqual(shaped.casefold().replace(",", "").replace(".", "").split(),
                         text.casefold().replace(",", "").replace(".", "").split(), "a word changed")

    def test_technical_profiles(self):
        for profile in ("claude-code", "vscode"):
            self.check(profile, "lista os ficheiros do módulo verde", "Lista os ficheiros do módulo verde.")
            self.check(profile, "revê o prompt...", "Revê o prompt.")
            self.check(profile, "abre o log, depois o readme,", "Abre o log, depois o readme.")
            self.check(profile, "qual é mais seguro? faz merge", "Qual é mais seguro? Faz merge.")
            self.check(profile, "kubectl está parado", "kubectl está parado.", keep=("kubectl",))

    def test_informal_profile(self):
        self.check("whatsapp", "o gato laranja fugiu outra vez.", "O gato laranja fugiu outra vez")
        self.check("whatsapp", "trazes as cadeiras azuis?", "Trazes as cadeiras azuis?")
        self.check("whatsapp", "parabéns! merecias mesmo!", "Parabéns! Merecias mesmo!")
        self.check("whatsapp", "logo se vê...", "Logo se vê...")
        self.check("whatsapp", "o bolo ficou torto. manda foto,", "O bolo ficou torto. Manda foto")

    def test_full_sentences_profile(self):
        self.check("email", "bom dia segue o mapa das salas", "Bom dia, segue o mapa das salas.")
        self.check("email", "olá a todos a horta precisa de água", "Olá a todos, a horta precisa de água.")
        self.check("email", "boa tarde, o estendal chegou", "Boa tarde, o estendal chegou.")
        self.check("email", "Boa tarde. O estendal chegou", "Boa tarde. O estendal chegou.")
        self.check("email", "boa tarde a equipa do xadrez ganhou", "Boa tarde a equipa do xadrez ganhou.")
        self.check("email", "obrigado pelas bolachas... amanhã devolvo a caixa", "Obrigado pelas bolachas... amanhã devolvo a caixa.")
        self.check("email", "fica para a próxima...", "Fica para a próxima.")
        self.check("email", "olá", "Olá.")

    def test_default_profile_matches_the_cleanup_punctuation(self):
        for text in ("abre o painel", "abre o painel, e depois fecha", "o que é isto?"):
            self.check(DEFAULT, text, clean_text(text))
        self.check(DEFAULT, "logo se vê...", "Logo se vê...")

    def test_empty_and_unknown(self):
        for profile in profiles.PROFILES:
            self.assertEqual(apply_profile("   ", profile), "")
        with self.assertRaises(ValueError):
            apply_profile("olá", "slack")


class StyleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "style"
        self.dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_samples_per_profile_with_default_fallback(self):
        self.assertEqual(load_style_samples(self.dir, "email"), ())
        self.assertEqual(load_style_samples(self.dir.parent / "absent", "email"), ())
        (self.dir / "whatsapp.txt").write_text("bora lá\nlogo vejo\n\n\nja chego", encoding="utf-8")
        (self.dir / "default.txt").write_text("Texto inventado.", encoding="utf-8")
        self.assertEqual(load_style_samples(self.dir, "whatsapp"), ("bora lá logo vejo", "ja chego"))
        self.assertEqual(load_style_samples(self.dir, "email"), ("Texto inventado.",))
        self.assertEqual(load_style_samples(self.dir, DEFAULT), ("Texto inventado.",))

    def test_limits(self):
        blocks = [f"amostra {index} " + "x" * 600 for index in range(8)]
        (self.dir / "email.txt").write_text("\r\n\r\n".join(blocks), encoding="utf-8")
        samples = load_style_samples(self.dir, "email")
        self.assertLessEqual(len(samples), profiles.MAX_SAMPLES)
        self.assertTrue(all(len(sample) <= profiles.MAX_SAMPLE_CHARS for sample in samples))
        self.assertLessEqual(sum(map(len, samples)), profiles.MAX_STYLE_CHARS)

    def test_unreadable_file_names_the_file_never_its_text(self):
        (self.dir / "email.txt").write_bytes("segredo pessoal \xff".encode("latin-1"))
        with self.assertRaises(StyleError) as caught:
            load_style_samples(self.dir, "email")
        self.assertIn("style/email.txt", str(caught.exception))
        self.assertNotIn("segredo", str(caught.exception))

    def test_prompt(self):
        self.assertEqual(style_prompt(DEFAULT), "")
        prompt = style_prompt("whatsapp", ("bora lá",))
        self.assertIn("informal chat message", prompt)
        self.assertIn("- bora lá", prompt)
        self.assertIn("technical term", style_prompt("vscode"))
        self.assertIn("complete sentences", style_prompt("email"))


class Reply:
    def __init__(self, content):
        self.content = content


class FakeClient:
    def __init__(self, content):
        self.content = content
        self.systems = []

    def chat(self, model, system, user, max_tokens=0):
        self.systems.append(system)
        return Reply(self.content)


class StyledCleanupTest(unittest.TestCase):
    def test_style_goes_only_into_the_llm_prompt(self):
        style = style_prompt("whatsapp", ("bora lá",))
        client = FakeClient("o comboio amarelo atrasou")
        result = Cleanup("llm", client=client, style=style)("hum o comboio amarelo atrasou")
        self.assertEqual(result.mode, "llm")
        self.assertTrue(client.systems[0].endswith(style))
        self.assertNotIn("bora", Cleanup("llm", client=client).system_prompt())
        # The rules mode never calls a model, so style samples cannot change its text.
        rules = Cleanup("rules", style=style)("hum o comboio amarelo atrasou")
        self.assertEqual(rules.text, clean_text("hum o comboio amarelo atrasou"))


class CliTest(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = profiles.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_prints_counts_only(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "email.txt").write_text("Texto inventado de exemplo.", encoding="utf-8")
            with mock.patch("quill.config.load_config", return_value=load_config(None)):
                code, out, _ = self.run_main("--check", folder)
        self.assertEqual(code, 0)
        self.assertIn("email 1", out)
        self.assertIn("not used (cleanup mode is rules)", out)
        self.assertNotIn("inventado", out)
        self.assertEqual(self.run_main()[0], 2)


class EnterRefusalTest(unittest.TestCase):
    def test_enter_needs_claude_code_and_no_new_dirty_marker(self):
        before = window("Code.exe", title="a.py - alpha - Visual Studio Code [Claude Code]")
        self.assertIsNone(EXAMPLE.enter_refusal(before, before))
        self.assertIsNone(EXAMPLE.enter_refusal(before, window("Code.exe", title=before.title + " - Modified")))
        self.assertEqual(EXAMPLE.enter_refusal(before, None), profiles.GONE)
        self.assertEqual(EXAMPLE.enter_refusal(before, window("Code.exe", title="a.py - alpha - Visual Studio Code "
                                                                                 "[Text Editor]")),
                         profiles.NO_LONGER_CLAUDE)
        dirty = window("Code.exe", title="● a.py - alpha - Visual Studio Code [Claude Code]")
        self.assertEqual(EXAMPLE.enter_refusal(before, dirty), profiles.BECAME_DIRTY)
        self.assertEqual(EXAMPLE.enter_refusal(None, dirty), profiles.BECAME_DIRTY)
        self.assertIsNone(EXAMPLE.enter_refusal(dirty, dirty))  # already dirty before typing
        terminal = window(TERMINAL, title="✳ Claude Code")
        self.assertIsNone(EXAMPLE.enter_refusal(terminal, terminal))
        self.assertEqual(EXAMPLE.enter_refusal(terminal, window(TERMINAL, title="PowerShell")),
                         profiles.NO_LONGER_CLAUDE)


EDITOR_TAB = "alpha | Invented topic - Visual Studio Code []"


def focused(title=EDITOR_TAB, verdict=uia.CLAUDE_CODE_INPUT, process="Code.exe"):
    return WindowInfo(process, "Chrome_WidgetWin_1", title, focus=verdict)


class FocusVerdictTest(unittest.TestCase):
    """A VS Code window without the marker is Claude Code only when its focused element is the message input."""

    def test_only_the_claude_code_input_verdict_makes_claude_code(self):
        for title in (EDITOR_TAB, "alpha | Invented topic - Visual Studio Code", "Invented topic - alpha - "
                      "Visual Studio Code [] - Modified", "alpha - Visual Studio Code [Text Editor]"):
            self.assertEqual(EXAMPLE.select(focused(title)), "claude-code", title)
            self.assertTrue(EXAMPLE.is_claude_code(focused(title)))
        for verdict in (uia.TEXT_EDITOR, uia.TERMINAL, uia.OTHER, uia.UNAVAILABLE, uia.TIMEOUT, None, "", "bogus"):
            self.assertEqual(EXAMPLE.select(focused(verdict=verdict)), "vscode", verdict)
            self.assertFalse(EXAMPLE.is_claude_code(focused(verdict=verdict)))

    def test_the_verdict_counts_only_in_vscode(self):
        self.assertEqual(EXAMPLE.select(focused(process="notepad.exe")), DEFAULT)
        self.assertEqual(EXAMPLE.select(focused(process="")), DEFAULT)  # unreadable process
        self.assertEqual(EXAMPLE.select(focused(title="PowerShell", process=TERMINAL)), DEFAULT)

    def test_needs_focus_only_where_it_could_change_the_profile(self):
        self.assertTrue(EXAMPLE.needs_focus(window("Code.exe", title=EDITOR_TAB)))
        self.assertTrue(EXAMPLE.needs_focus(window("code.EXE", title="")))
        self.assertFalse(EXAMPLE.needs_focus(window("Code.exe", title="a - Visual Studio Code [Claude Code]")))
        self.assertFalse(EXAMPLE.needs_focus(window(TERMINAL, title="✳ Claude Code")))
        self.assertFalse(EXAMPLE.needs_focus(window("notepad.exe")))
        self.assertFalse(EXAMPLE.needs_focus(None))

    def test_enter_after_typing_needs_the_input_focused_again(self):
        before = focused()
        self.assertIsNone(EXAMPLE.enter_refusal(before, focused()))
        for verdict in (uia.TEXT_EDITOR, uia.TERMINAL, uia.OTHER, uia.UNAVAILABLE, uia.TIMEOUT, None):
            self.assertEqual(EXAMPLE.enter_refusal(before, focused(verdict=verdict)), profiles.LEFT_INPUT, verdict)
        # Even when the title now says Claude Code: the window was recognised by its focus.
        marked = focused(title="alpha - Visual Studio Code [Claude Code]", verdict=uia.OTHER)
        self.assertEqual(EXAMPLE.enter_refusal(before, marked), profiles.LEFT_INPUT)
        self.assertEqual(EXAMPLE.enter_refusal(before, None), profiles.GONE)
        dirty = focused(title="● a.py - " + EDITOR_TAB)
        self.assertEqual(EXAMPLE.enter_refusal(before, dirty), profiles.BECAME_DIRTY)


class FocusWindows(FakeWindows):
    def __init__(self):
        super().__init__()
        self.titles[10] = EDITOR_TAB
        self.windows[40] = 4
        self.images[4] = "C:\\Invented\\notepad.exe"


class Probe:
    """A fake ``quill.uia.FocusProbe``: one verdict, or an error; records the windows asked."""

    def __init__(self, verdict=uia.CLAUDE_CODE_INPUT, error=None):
        self.verdict = verdict
        self.error = error
        self.asked = []
        self.closed = False

    def __call__(self, hwnd):
        self.asked.append(hwnd)
        if self.error is not None:
            raise self.error
        return self.verdict

    def close(self):
        self.closed = True


class DescribeTest(unittest.TestCase):
    def setUp(self):
        self.api = FocusWindows()

    def test_the_editor_tab_is_asked_and_recognised(self):
        probe = Probe()
        info = EXAMPLE.describe(self.api, 10, probe)
        self.assertEqual((info.focus, EXAMPLE.select(info)), (uia.CLAUDE_CODE_INPUT, "claude-code"))
        self.assertEqual(probe.asked, [10])

    def test_no_question_where_the_answer_changes_nothing(self):
        probe = Probe()
        self.api.titles[10] = "alpha - Visual Studio Code [Claude Code]"
        self.assertIsNone(EXAMPLE.describe(self.api, 10, probe).focus)  # the title already says Claude Code
        self.assertIsNone(EXAMPLE.describe(self.api, 20, probe).focus)  # a terminal
        self.assertIsNone(EXAMPLE.describe(self.api, 40, probe).focus)  # another program
        self.assertIsNone(EXAMPLE.describe(self.api, 99, probe))  # gone
        self.assertEqual(probe.asked, [])
        self.assertIsNone(EXAMPLE.describe(self.api, 10, None, force_focus=True).focus)  # no probe at all
        # Before an Enter, a window recognised by its focus is asked again even with the marker.
        self.assertEqual(EXAMPLE.describe(self.api, 10, probe, force_focus=True).focus, uia.CLAUDE_CODE_INPUT)
        self.assertIsNone(EXAMPLE.describe(self.api, 20, probe, force_focus=True).focus)  # never a terminal
        self.assertEqual(probe.asked, [10])

    def test_a_failing_or_odd_probe_is_never_claude_code(self):
        with self.assertLogs("quill.profiles", level="WARNING") as logs:
            info = EXAMPLE.describe(self.api, 10, Probe(error=RuntimeError("fake: zebracanary")))
        self.assertEqual((info.focus, EXAMPLE.select(info)), (uia.UNAVAILABLE, "vscode"))
        self.assertIn("focus check failed (RuntimeError)", "\n".join(logs.output))
        self.assertNotIn("zebracanary", "\n".join(logs.output))
        info = EXAMPLE.describe(self.api, 10, Probe(verdict=None))
        self.assertEqual((info.focus, EXAMPLE.select(info)), (uia.UNAVAILABLE, "vscode"))


# Invented words that must never reach the probe's output (titles, project names, paths).
CANARIES = ("zebracanary", "quokkacanary", "lemurcanary", "Invented")


class ProbeWindows(FakeWindows):
    """The read-only queries of the probe; any other call (input, focus, window changes) fails the test."""

    def __init__(self):
        super().__init__()
        self.windows = {10: 1, 11: 1, 12: 1, 20: 2, 30: 3, 40: 1}
        self.images = {1: "C:\\Invented\\lemurcanary\\Code.exe", 2: "C:\\Invented\\WindowsTerminal.exe",
                       3: "C:\\Invented\\notepad.exe"}
        self.classes = {hwnd: "Chrome_WidgetWin_1" for hwnd in (10, 11, 12, 40)}
        self.classes.update({20: "CASCADIA_HOSTING_WINDOW_CLASS", 30: "Notepad"})
        self.titles = {
            10: "● quokkacanary.py - zebracanary - Visual Studio Code [Claude Code] - Modified",
            11: "zebracanary | quokkacanary.md - Visual Studio Code [Text Editor] - Modified",
            12: "zebracanary | quokkacanary topic - Visual Studio Code []",
            20: "✳ Claude Code zebracanary",
            30: "zebracanary notes - Notepad",
            40: "",
        }
        self.visible = {10, 11, 12, 20, 30, 40}
        self.foreground = 10

    def __getattr__(self, name):  # only the read queries above exist: send_input, focus calls, ... fail
        raise AssertionError(f"the probe called {name}")

    def foreground_window(self):
        return self.foreground

    def top_level_windows(self):
        return list(self.windows)

    def is_visible(self, hwnd):
        return hwnd in self.visible


class ProbeTest(unittest.TestCase):
    def setUp(self):
        from quill.projects import ProjectDetector, ProjectFolders

        self.api = ProbeWindows()
        folders = ProjectFolders([("zebracanary", "C:\\Invented\\zebracanary")], is_dir=lambda path: True,
                                 drives=lambda drive: 3)
        self.detector = ProjectDetector(folders, None)
        self.focus = Probe(uia.OTHER)

    def run_main(self, *argv, sleeps=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("quill.config.load_config", return_value=load_config(None)):
            code = profiles.main(list(argv), parts=lambda config: (self.api, self.detector, self.focus),
                                 sleep=sleeps.append if sleeps is not None else self.fail)
        return code, out.getvalue(), err.getvalue()

    def assert_private(self, text):
        for canary in CANARIES:
            self.assertNotIn(canary.casefold(), text.casefold())

    def test_foreground_window(self):
        code, out, err = self.run_main("--probe")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "foreground: process Code.exe; class Chrome_WidgetWin_1; profile claude-code; "
                                      "rule claude-code #2 (process+title); marker claude-code; state suffix yes; "
                                      "focus other; project vscode_title; mouse 5 Enter yes")
        self.assert_private(out + err)
        self.assertEqual((self.focus.asked, self.focus.closed), ([10], True))

    def test_the_focused_claude_code_editor_tab(self):
        self.api.foreground = 12
        self.focus.verdict = uia.CLAUDE_CODE_INPUT
        out = self.run_main("--probe")[1]
        self.assertEqual(out.strip(), "foreground: process Code.exe; class Chrome_WidgetWin_1; profile claude-code; "
                                      "rule claude-code (focus); marker empty; state suffix no; "
                                      "focus claude_code_input; project vscode_title; mouse 5 Enter yes")
        for verdict in (uia.TEXT_EDITOR, uia.TERMINAL, uia.OTHER, uia.UNAVAILABLE, uia.TIMEOUT):
            self.focus.verdict = verdict
            out = self.run_main("--probe")[1]
            self.assertIn(f"profile vscode; rule vscode #1 (process); marker empty; state suffix no; focus {verdict}; ",
                          out)
            self.assertIn("mouse 5 Enter no", out)
        self.assert_private(out)

    def test_focus_check_off(self):
        self.api.foreground = 12
        self.focus = None
        out = self.run_main("--probe")[1]
        self.assertIn("profile vscode; rule vscode #1 (process); marker empty; state suffix no; focus n/a; ", out)

    def test_every_vscode_and_terminal_window(self):
        code, out, err = self.run_main("--probe", "--all")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)  # notepad and the untitled VS Code window are not listed
        self.assertTrue(lines[0].startswith("window 1 (foreground): "))
        self.assertIn("profile claude-code; rule claude-code #2 (process+title); marker claude-code; "
                      "state suffix yes; focus other", lines[0])
        self.assertIn("profile vscode; rule vscode #1 (process); marker other; state suffix yes; "
                      "focus n/a; project vscode_title; mouse 5 Enter no", lines[1])
        self.assertIn("profile vscode; rule vscode #1 (process); marker empty; state suffix no; "
                      "focus n/a; project vscode_title; mouse 5 Enter no", lines[2])
        self.assertIn("process WindowsTerminal.exe; class CASCADIA_HOSTING_WINDOW_CLASS; profile claude-code; "
                      "rule claude-code #1 (process+title); marker none; state suffix no; focus n/a; "
                      "project terminal_title; mouse 5 Enter yes", lines[3])
        self.assertEqual(self.focus.asked, [10])  # the keyboard focus is in the foreground window only
        self.assert_private(out + err)

    def test_delay_unreadable_and_gone_windows(self):
        sleeps = []
        self.api.foreground = 30
        code, out, _ = self.run_main("--probe", "--delay", "2.5", sleeps=sleeps)
        self.assertEqual((code, sleeps), (0, [2.5]))
        self.assertIn("profile default; rule none; marker none; state suffix no; focus n/a; project n/a; "
                      "mouse 5 Enter no", out)
        self.api.foreground = 99
        self.assertIn("foreground: window gone", self.run_main("--probe")[1])
        self.api.foreground = 0
        self.assertIn("foreground: no window", self.run_main("--probe")[1])
        self.api.foreground = 10
        del self.api.images[1]  # an elevated VS Code: the process cannot be read, so no process list matches
        out = self.run_main("--probe")[1]
        self.assertIn("process (unreadable); class Chrome_WidgetWin_1; profile default", out)
        self.assertIn("mouse 5 Enter no", out)
        self.assert_private(out)

    def test_bad_arguments(self):
        for argv in ((), ("--delay", "3"), ("--all",), ("--probe", "--delay", "61"), ("--probe", "--delay", "-1"),
                     ("--probe", "--delay", "nan"), ("--probe", "--check")):
            self.assertEqual(self.run_main(*argv)[0], 2, argv)

    def test_probe_unavailable(self):
        out, err = io.StringIO(), io.StringIO()

        def broken(config):
            raise OSError("fake: no Win32")

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("quill.config.load_config", return_value=load_config(None)):
            self.assertEqual(profiles.main(["--probe"], parts=broken), 1)
        self.assertIn("window probe unavailable (OSError)", err.getvalue())


if __name__ == "__main__":
    unittest.main()
