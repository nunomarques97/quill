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

from quill import profiles
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
        # The marker counts only in VS Code; Windows Terminal keeps its plain title rule.
        self.assertFalse(EXAMPLE.is_claude_code(window("chrome.exe", title="Page [Claude Code]")))
        self.assertTrue(EXAMPLE.is_claude_code(window(TERMINAL, title="Claude Code")))

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


if __name__ == "__main__":
    unittest.main()
