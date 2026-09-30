"""Configuration loading and validation with invented values in temporary files."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from quill import config
from quill.config import ConfigError, load_config


class ConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def local(self, text: str) -> Path:
        path = self.folder / "quill.toml"
        path.write_text(text, "utf-8")
        return path

    def load(self, text: str) -> config.Config:
        return load_config(self.local(text))

    def rejected(self, text: str, field: str, *hidden: str) -> str:
        """Assert the file is refused with a message naming ``field`` and none of ``hidden``."""
        with self.assertRaises(ConfigError) as caught:
            self.load(text)
        message = str(caught.exception)
        self.assertIn(field, message)
        for value in hidden:
            self.assertNotIn(value, message)
        return message


class ExampleTest(ConfigCase):
    def test_example_alone_is_valid(self) -> None:
        settings = load_config(None)
        dictation = settings.trigger("dictation")
        self.assertEqual([(item.kind, item.name) for item in dictation.inputs],
                         [("button", "xbutton1"), ("key", "f13"), ("key", "right_ctrl")])
        self.assertEqual([item.name for item in settings.trigger("send_claude").inputs], ["xbutton2", "f15"])
        self.assertEqual([item.name for item in settings.trigger("command").inputs], ["f14"])
        self.assertEqual(settings.min_hold_ms, 250)
        self.assertTrue(settings.click_to_focus)
        self.assertEqual(settings.indicator_position, "pointer")
        self.assertEqual(settings.engine_model, "large-v3-turbo")
        self.assertEqual(settings.ollama_url, "http://127.0.0.1:11434")
        self.assertEqual(settings.ollama_model, "qwen3:8b")
        self.assertEqual(settings.cleanup_mode, "rules")
        self.assertEqual([profile.name for profile in settings.profiles],
                         ["claude-code", "claude-code", "vscode", "whatsapp", "email"])
        # Claude Code in VS Code needs the [Claude Code] marker of the window.title setting.
        self.assertEqual([(profile.processes, profile.titles) for profile in settings.profiles[:2]],
                         [(("WindowsTerminal.exe",), ("Claude Code",)), (("Code.exe",), ("[Claude Code]",))])
        local = config.LOCAL_DIR.resolve()
        for path in (settings.vocabulary_path, settings.corrections_path, settings.style_dir):
            self.assertTrue(path.is_relative_to(local))

    def test_autorewrite_defaults(self) -> None:
        rewrite = load_config(None).autorewrite
        self.assertEqual((rewrite.enabled, rewrite.min_audio_s, rewrite.min_words, rewrite.timeout_s),
                         (True, 15.0, 40, 4.0))
        self.assertEqual((rewrite.undo_key.name, rewrite.undo_key.vk, rewrite.undo_window_s), ("f17", 0x80, 30))

    def test_claude_alert_defaults(self) -> None:
        alert = load_config(None).claude_alert
        self.assertEqual((alert.enabled, alert.sound, alert.filter), (True, True, "attended"))

    def test_action_for_maps_inputs_to_actions(self) -> None:
        settings = load_config(None)
        self.assertEqual(settings.action_for("button", 0x05), "dictation")
        self.assertEqual(settings.action_for("button", 0x06), "send_claude")
        self.assertEqual(settings.action_for("key", 0x7C), "dictation")  # F13
        self.assertEqual(settings.action_for("key", 0xA3), "dictation")  # Right Ctrl
        self.assertIsNone(settings.action_for("key", 0xA2))  # Left Ctrl is never a trigger
        self.assertIsNone(settings.action_for("key", 0x05))  # a key code is not a button

    def test_missing_local_file_uses_the_example(self) -> None:
        self.assertEqual(load_config(self.folder / "absent.toml"), load_config(None))


class MergeTest(ConfigCase):
    def test_local_values_override_only_what_they_name(self) -> None:
        settings = self.load("[input]\nmin_hold_ms = 400\n[triggers.command]\nkeys = [\"f20\"]\n")
        self.assertEqual(settings.min_hold_ms, 400)
        self.assertTrue(settings.click_to_focus)
        self.assertEqual([item.name for item in settings.trigger("command").inputs], ["f20"])
        self.assertEqual([item.name for item in settings.trigger("dictation").inputs],
                         ["xbutton1", "f13", "right_ctrl"])

    def test_local_profile_replaces_the_example_profile_whole(self) -> None:
        settings = self.load("[profiles.claude-code]\ntitles = [\"Invented Agent\"]\n")
        profile = settings.profiles[0]
        self.assertEqual((profile.name, profile.processes, profile.titles), ("claude-code", (), ("Invented Agent",)))
        self.assertEqual(len(settings.profiles), 4)

    def test_profile_alternatives_are_kept_in_order(self) -> None:
        settings = self.load("[[profiles.vscode]]\nprocesses = [\"Invented.exe\"]\n"
                             "[[profiles.vscode]]\nclasses = [\"InventedClass\"]\ntitles = [\"Invented\"]\n")
        vscode = [profile for profile in settings.profiles if profile.name == "vscode"]
        self.assertEqual([(p.processes, p.classes, p.titles) for p in vscode],
                         [(("Invented.exe",), (), ()), ((), ("InventedClass",), ("Invented",))])
        self.assertEqual([p.name for p in settings.profiles],
                         ["claude-code", "claude-code", "vscode", "vscode", "whatsapp", "email"])
        # A local table replaces both claude-code alternatives of the example.
        replaced = self.load("[profiles.claude-code]\nprocesses = [\"Invented.exe\"]\ntitles = [\"Agent\"]\n")
        self.assertEqual([p.name for p in replaced.profiles], ["claude-code", "vscode", "whatsapp", "email"])

    def test_all_function_keys_and_right_modifiers_are_accepted(self) -> None:
        keys = [f"f{number}" for number in range(13, 25)]
        settings = self.load(
            "[triggers.dictation]\nbuttons = []\nkeys = [" + ", ".join(f'"{key}"' for key in keys) + "]\n"
            "[triggers.command]\nbuttons = [\"middle\"]\nkeys = [\"right_alt\"]\n"
            "[triggers.send_claude]\nbuttons = [\"xbutton1\", \"xbutton2\"]\nkeys = [\"right_ctrl\"]\n")
        self.assertEqual([item.vk for item in settings.trigger("dictation").inputs], list(range(0x7C, 0x88)))

    def test_precise_engine_model_is_selectable(self) -> None:
        self.assertEqual(self.load("[engine]\nmodel = \"large-v3\"\n").engine_model, "large-v3")

    def test_command_and_send_triggers_can_be_disabled(self) -> None:
        settings = self.load("[triggers.command]\nkeys = []\n[triggers.send_claude]\nbuttons = []\nkeys = []\n")
        self.assertFalse(settings.trigger("command").enabled)
        self.assertFalse(settings.trigger("send_claude").enabled)


class RejectTest(ConfigCase):
    def test_unknown_field_names_are_rejected(self) -> None:
        self.rejected("[input]\nhold_ms = 3\n", "input.hold_ms")
        self.rejected("[extra]\nx = 1\n", "unknown field extra")
        self.rejected("[triggers.dictation]\nkey = [\"f13\"]\n", "triggers.dictation.key")
        self.rejected("[triggers.dictate]\nkeys = [\"f16\"]\n", "triggers.dictate")
        self.rejected("[profiles.slack]\nprocesses = [\"x.exe\"]\n", "profiles.slack")
        self.rejected("[profiles.email]\nprocess = [\"x.exe\"]\n", "profiles.email.process")

    def test_unknown_key_and_button_names_are_rejected(self) -> None:
        self.rejected("[triggers.dictation]\nkeys = [\"f25\"]\n", "triggers.dictation.keys[0]", "f25")
        self.rejected("[triggers.dictation]\nkeys = [\"F13\"]\n", "triggers.dictation.keys[0]")
        self.rejected("[triggers.command]\nkeys = [\"left_ctrl\"]\n", "triggers.command.keys[0]", "left_ctrl")
        self.rejected("[triggers.command]\nbuttons = [\"left\"]\n", "triggers.command.buttons[0]")
        self.rejected("[triggers.command]\nbuttons = [5]\n", "triggers.command.buttons[0]")
        self.rejected("[triggers.command]\nkeys = \"f14\"\n", "triggers.command.keys")

    def test_one_input_bound_to_two_actions_is_rejected(self) -> None:
        message = self.rejected("[triggers.command]\nkeys = [\"f13\"]\n", "triggers.command.keys[0]", "f13")
        self.assertIn("triggers.dictation.keys[0]", message)
        self.rejected("[triggers.command]\nbuttons = [\"xbutton2\"]\nkeys = []\n", "triggers.send_claude.buttons[0]")
        self.rejected("[triggers.dictation]\nkeys = [\"f16\", \"f16\"]\n", "triggers.dictation.keys[1]")

    def test_dictation_needs_an_input(self) -> None:
        self.rejected("[triggers.dictation]\nbuttons = []\nkeys = []\n", "triggers.dictation")

    def test_non_loopback_ollama_urls_are_rejected(self) -> None:
        for url in ("http://192.168.1.20:11434", "http://example.invalid:11434", "https://127.0.0.1:11434",
                    "http://127.0.0.1.example.invalid:11434", "http://someone@127.0.0.1:11434",
                    "http://127.0.0.1:11434/api/chat", "http://127.0.0.1:11434/?next=x", "http://0.0.0.0:11434",
                    "http://127.0.0.1:99999", "ftp://127.0.0.1", "127.0.0.1:11434", "http://[::ffff:10.0.0.1]:1"):
            self.rejected(f"[ollama]\nurl = \"{url}\"\n", "ollama.url", url)
        self.rejected("[ollama]\nurl = 11434\n", "ollama.url")

    def test_loopback_ollama_urls_are_accepted(self) -> None:
        for url, expected in (("http://localhost:11434/", "http://localhost:11434"),
                              ("http://127.0.0.2:8080", "http://127.0.0.2:8080"),
                              ("http://[::1]:11434", "http://[::1]:11434")):
            self.assertEqual(self.load(f"[ollama]\nurl = \"{url}\"\n").ollama_url, expected)

    def test_personal_data_paths_must_stay_inside_local(self) -> None:
        for field in ("vocabulary", "corrections", "style"):
            for value in ("bench/vocabulary.toml", "../outside.json", "local/../bench/x.json", "local",
                          "C:/data/x.json", "/data/x.json", "C:x.json", "\\\\\\\\server\\\\share\\\\x.json", ""):
                self.rejected(f"[paths]\n{field} = \"{value}\"\n", f"paths.{field}")
        settings = self.load("[paths]\nvocabulary = \"local/sub/words.toml\"\n")
        self.assertEqual(settings.vocabulary_path, (config.LOCAL_DIR / "sub" / "words.toml").resolve())

    def test_values_are_type_checked(self) -> None:
        self.rejected("[input]\nmin_hold_ms = true\n", "input.min_hold_ms")
        self.rejected("[input]\nmin_hold_ms = 10\n", "input.min_hold_ms")
        self.rejected("[input]\nmin_hold_ms = 5000\n", "input.min_hold_ms")
        self.rejected("[input]\nclick_to_focus = \"yes\"\n", "input.click_to_focus")
        self.rejected("[indicator]\nposition = \"top\"\n", "indicator.position")
        self.rejected("[cleanup]\nmode = \"cloud\"\n", "cleanup.mode")
        self.rejected("[engine]\nmodel = \"tiny\"\n", "engine.model")
        self.rejected("[engine]\nmodel = 3\n", "engine.model")
        self.rejected("[ollama]\nmodel = \"qwen3:8b; rm\"\n", "ollama.model", "rm")
        self.rejected("[profiles.vscode]\n", "profiles.vscode")
        self.rejected("[profiles.vscode]\ntitles = [\"\"]\n", "profiles.vscode.titles[0]")
        self.rejected("profiles.vscode = []\n", "profiles.vscode")
        self.rejected("[[profiles.vscode]]\nprocesses = [\"x.exe\"]\n[[profiles.vscode]]\n", "profiles.vscode[1]")
        self.rejected("[[profiles.vscode]]\nprocess = [\"x.exe\"]\n", "profiles.vscode[0].process")
        self.rejected("[[profiles.vscode]]\ntitles = [3]\n", "profiles.vscode[0].titles[0]")
        self.rejected("profiles.vscode = [3]\n", "profiles.vscode[0]")
        self.rejected("[[profiles.slack]]\nprocesses = [\"x.exe\"]\n", "profiles.slack")
        self.rejected("input = 3\n", "input")
        self.rejected("[input.min_hold_ms]\nx = 1\n", "input.min_hold_ms")

    def test_autorewrite_values(self) -> None:
        rewrite = self.load("[autorewrite]\nenabled = true\nmin_audio_s = 12.5\nmin_words = 30\ntimeout_s = 2\n").autorewrite
        self.assertEqual((rewrite.enabled, rewrite.min_audio_s, rewrite.min_words, rewrite.timeout_s),
                         (True, 12.5, 30, 2.0))
        for text, field in (
            ("enabled = 1", "autorewrite.enabled"),
            ('enabled = "yes"', "autorewrite.enabled"),
            ("min_audio_s = 2", "autorewrite.min_audio_s"),
            ("min_audio_s = 500", "autorewrite.min_audio_s"),
            ('min_audio_s = "15"', "autorewrite.min_audio_s"),
            ("min_audio_s = true", "autorewrite.min_audio_s"),
            ("min_words = 5", "autorewrite.min_words"),
            ("min_words = 40.5", "autorewrite.min_words"),
            ("min_words = false", "autorewrite.min_words"),
            ("timeout_s = 0", "autorewrite.timeout_s"),
            ("timeout_s = 60", "autorewrite.timeout_s"),
            ("undo = 1", "autorewrite.undo"),
            ('undo_key = "f25"', "autorewrite.undo_key"),
            ('undo_key = "F17"', "autorewrite.undo_key"),
            ("undo_key = 17", "autorewrite.undo_key"),
            ('undo_key = "f13"', "autorewrite.undo_key"),  # a trigger key
            ('undo_key = "f16"', "autorewrite.undo_key"),  # the correction key
            ("undo_window_s = 2", "autorewrite.undo_window_s"),
            ("undo_window_s = 700", "autorewrite.undo_window_s"),
            ("undo_window_s = 30.5", "autorewrite.undo_window_s"),
            ("undo_window_s = true", "autorewrite.undo_window_s"),
        ):
            with self.subTest(text=text):
                message = self.rejected(f"[autorewrite]\n{text}\n", field)
                self.assertTrue(message.isascii())
        self.assertIn("correction key", self.rejected('[autorewrite]\nundo_key = "f16"\n', "autorewrite.undo_key"))

    def test_claude_alert_values(self) -> None:
        alert = self.load('[claude_alert]\nenabled = false\nsound = false\nfilter = "unless-headless"\n').claude_alert
        self.assertEqual((alert.enabled, alert.sound, alert.filter), (False, False, "unless-headless"))
        self.assertEqual(self.load('[claude_alert]\nfilter = "all"\n').claude_alert.filter, "all")
        for text, field in (
            ("enabled = 1", "claude_alert.enabled"),
            ('sound = "yes"', "claude_alert.sound"),
            ('filter = "headless"', "claude_alert.filter"),
            ('filter = "Attended"', "claude_alert.filter"),
            ("filter = 1", "claude_alert.filter"),
            ("volume = 3", "claude_alert.volume"),
        ):
            with self.subTest(text=text):
                self.assertTrue(self.rejected(f"[claude_alert]\n{text}\n", field).isascii())

    def test_undo_key_values(self) -> None:
        rewrite = self.load('[autorewrite]\nundo_key = "f18"\nundo_window_s = 60\n').autorewrite
        self.assertEqual((rewrite.undo_key.name, rewrite.undo_window_s), ("f18", 60))
        self.assertIsNone(self.load('[autorewrite]\nundo_key = ""\n').autorewrite.undo_key)
        # The correction key's key is free for the undo key once the correction key moves or is off.
        moved = self.load('[corrections]\nkey = ""\n[autorewrite]\nundo_key = "f16"\n')
        self.assertEqual((moved.correction_key, moved.autorewrite.undo_key.name), (None, "f16"))

    def test_the_example_undo_key_gives_way_to_a_local_trigger_or_correction_key(self) -> None:
        taken = self.load('[triggers.command]\nkeys = ["f17"]\n')
        self.assertIsNone(taken.autorewrite.undo_key)
        self.assertEqual(taken.correction_key.name, "f16")
        moved = self.load('[corrections]\nkey = "f17"\n')
        self.assertEqual((moved.correction_key.name, moved.autorewrite.undo_key), ("f17", None))
        # A local undo key is never silently dropped.
        self.rejected('[triggers.command]\nkeys = ["f18"]\n[autorewrite]\nundo_key = "f18"\n', "autorewrite.undo_key")

    def test_microphone_value_never_appears_in_errors(self) -> None:
        self.rejected("[audio]\nmicrophone = \"Invented Mic\\u0007Name\"\n", "audio.microphone", "Invented")
        self.rejected("[audio]\nmicrophone = \"   \"\n", "audio.microphone")
        self.rejected("[audio]\nmicrophone = 3\n", "audio.microphone")

    def test_invalid_toml_is_a_config_error(self) -> None:
        self.rejected("[input\nmin_hold_ms = 1\n", "not valid TOML")


class CliTest(ConfigCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = config.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_check_example_exits_zero(self) -> None:
        code, out, _ = self.run_cli("--check", str(config.EXAMPLE_CONFIG))
        self.assertEqual(code, 0)
        self.assertIn("OK (quill.example.toml", out)

    def test_invalid_file_exits_one_naming_the_field_only(self) -> None:
        path = self.local("[audio]\nmicrophone = 7\n[ollama]\nurl = \"http://203.0.113.9:1\"\n")
        code, out, err = self.run_cli("--check", str(path))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("audio.microphone", err)
        self.assertNotIn(str(self.folder), err)

    def test_missing_file_and_missing_option_exit_two(self) -> None:
        self.assertEqual(self.run_cli("--check", str(self.folder / "absent.toml"))[0], 2)
        self.assertEqual(self.run_cli()[0], 2)


if __name__ == "__main__":
    unittest.main()
