"""quill.voice tests: the command registry and "abre VS Code no <projeto>" with invented names.

The shortcuts are synthetic .lnk files in temporary folders and the launcher
is a fake: nothing is opened.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from quill import shortcuts as SC
from quill import voice
from quill.indicator.render import ERROR, VOICE_NONE, VOICE_OPEN
from quill.tests.test_shortcuts import CODE, FakeLauncher, write_link
from quill.vocabulary import Entry, Vocabulary
from quill.streaming import options_for
from quill.voice import Intent, OpenProject, Parser, VoiceCommands, VoiceHints, VoiceOutcome, normalize, voice_hints
from quill.whisper import PROMPT_MAX_CHARS, PROMPT_PART_MAX, SessionHints

NAMES = ("nimbus-deck", "nimbus-deck-public", "orla", "orla-public", "tarvo-kit", "velinor-app")
# Spoken names and folder paths that must never reach a log line.
PRIVATE = ("nimbus", "orla", "tarvo", "velinor", "zefiro", "Hub")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


class Dummy:
    """An invented command, added to the registry without changing the parser."""

    name = "dummy"

    def __init__(self) -> None:
        self.ran: list[Intent] = []

    def parse(self, text: str) -> Intent | None:
        if text.startswith("mostra o relogio"):
            return Intent(self.name, {"rest": text[len("mostra o relogio"):].strip()})
        return None

    def run(self, intent: Intent) -> VoiceOutcome:
        self.ran.append(intent)
        return VoiceOutcome(True, "shown", VOICE_OPEN, "Relógio", self.name)


class Broken(Dummy):
    name = "broken"

    def run(self, intent: Intent) -> VoiceOutcome:
        raise RuntimeError("zefiro secret text")


class NormalizeTest(unittest.TestCase):
    def test_case_accents_and_punctuation(self) -> None:
        self.assertEqual(normalize("  Abre o VS-Code, na Órla_Public!  "), "abre o vs code na orla public")
        self.assertEqual(normalize("Ábrir “VS Code” em Tárvo…"), "abrir vs code em tarvo")
        self.assertEqual(normalize("?!"), "")


class ParserTest(unittest.TestCase):
    def test_a_new_command_is_registered_without_changing_the_parser(self) -> None:
        parser = voice.default_parser([], Vocabulary, FakeLauncher())
        dummy = Dummy()
        parser.register(dummy)
        self.assertEqual(parser.names, ("open_project", "dummy"))
        command, intent = parser.parse("Mostra o relógio, agora.")
        self.assertIs(command, dummy)
        self.assertEqual(intent.args, {"rest": "agora"})
        outcome = VoiceCommands(parser, clock=Clock()).run("mostra o relógio")
        self.assertEqual((outcome.ok, outcome.command, outcome.text), (True, "dummy", "Relógio"))
        self.assertEqual(len(dummy.ran), 1)  # parse alone runs nothing

    def test_names_are_unique_and_order_is_kept(self) -> None:
        parser = Parser([Dummy()])
        with self.assertRaises(ValueError):
            parser.register(Dummy())
        self.assertIsNone(parser.parse(""))
        self.assertIsNone(parser.parse("   ,.  "))

    def test_unrecognized_text_does_nothing(self) -> None:
        dummy = Dummy()
        commands = VoiceCommands(Parser([dummy]), clock=Clock())
        for text in ("", "olá mundo", "abre o Chrome no orla", "escreve abre VS Code no orla"):
            with self.subTest(text=text):
                outcome = commands.run(text)
                self.assertEqual((outcome.ok, outcome.reason, outcome.state, outcome.text),
                                 (False, voice.UNRECOGNIZED, VOICE_NONE, "Comando não reconhecido"))
        self.assertEqual(dummy.ran, [])

    def test_a_failing_command_logs_its_type_only(self) -> None:
        commands = VoiceCommands(Parser([Broken()]), clock=Clock())
        with self.assertLogs("quill.voice", "INFO") as logs:
            outcome = commands.run("mostra o relógio")
        self.assertEqual((outcome.ok, outcome.reason, outcome.state), (False, voice.FAILED, ERROR))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("zefiro", "\n".join(logs.output))


class OpenProjectParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.command = OpenProject([], Vocabulary, FakeLauncher())

    def project(self, spoken: str) -> str | None:
        intent = self.command.parse(normalize(spoken))
        return None if intent is None else intent.args["project"]

    def test_spoken_forms(self) -> None:
        # With the article "o", these forms use "na", "em" or "num": the committed files avoid repeating
        # the private recording script word for word (bench.privacy_guard).
        for spoken in ("abre VS Code no Nimbus Deck", "Abre o VS Code na nimbus deck.", "abrir VS Code em Nimbus-Deck",
                       "Abre vscode no nimbus deck", "abre o V.S. Code em nimbus deck", "abre o vê esse code na "
                       "nimbus deck", "Abre Visual Studio Code no nimbus deck", "abre o visual studio na nimbus deck",
                       "abre o bs code num projeto nimbus deck", "abre VS Cod no nimbus deck, por favor",
                       "podes abrir o VS Code em repositório nimbus deck", "ABRE VS CODE NO NIMBUS DECK!",
                       "abre o vi es code na nimbus deck", "abre VS Code no o nimbus deck"):
            with self.subTest(spoken=spoken):
                self.assertEqual(self.project(spoken), "nimbus deck")

    def test_other_text_is_not_this_command(self) -> None:
        for spoken in ("abre VS Code", "abre VS Code no", "abre o Chrome na nimbus deck", "fecha VS Code no orla",
                       "VS Code no orla", "hoje abre VS Code no orla às nove e escrevo", "abre a pasta na orla"):
            with self.subTest(spoken=spoken):
                self.assertIsNone(self.project(spoken))


class OpenProjectRunTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.main = root / "Hub Main"
        self.other = root / "Hub Other"
        self.main.mkdir()
        self.other.mkdir()
        for name in NAMES:
            write_link(self.main, name, CODE, arguments=f'--new-window "C:\\Work\\{name}.code-workspace"')
        self.launcher = FakeLauncher()
        self.vocabulary = Vocabulary(names=(Entry("Tarvo Kit", "name", ("tarbo quit",)),))
        self.folders = [self.main, self.other, root / "absent"]
        self.vscode = None  # the real VS Code lookup (only called for a folder target)

    def commands(self) -> VoiceCommands:
        if self.vscode is None:
            parser = voice.default_parser(self.folders, lambda: self.vocabulary, self.launcher)
        else:
            parser = Parser([OpenProject(self.folders, lambda: self.vocabulary, self.launcher,
                                         launch=lambda item, launcher: SC.launch(item, launcher, vscode=self.vscode))])
        return VoiceCommands(parser, clock=Clock())

    def run_voice(self, spoken: str) -> tuple[VoiceOutcome, str]:
        with self.assertLogs("quill", "DEBUG") as logs:
            outcome = self.commands().run(spoken)
        text = "\n".join(logs.output)
        for private in PRIVATE:
            self.assertNotIn(private.casefold(), text.casefold())
        self.assertNotIn(str(self.main.parent), text)
        return outcome, text

    def test_an_exact_name_opens_its_shortcut_not_the_sibling(self) -> None:
        for spoken, name in (("abre VS Code no orla", "orla"), ("abre o VS Code na orla public", "orla-public"),
                             ("abrir vscode em Nimbus Deck", "nimbus-deck"),
                             ("abre o vê esse code no nimbus deck público", "nimbus-deck-public")):
            with self.subTest(spoken=spoken):
                self.launcher.opened.clear()
                outcome, logs = self.run_voice(spoken)
                self.assertEqual((outcome.ok, outcome.reason, outcome.state, outcome.text),
                                 (True, SC.OPENED, VOICE_OPEN, f"A abrir {name}"))
                self.assertEqual(self.launcher.opened, [self.main / f"{name}.lnk"])
                self.assertEqual(self.launcher.started, [])
                self.assertEqual(outcome.counts["shortcuts"], len(NAMES))
                self.assertEqual(outcome.counts["folders_failed"], 1)  # the absent folder is skipped
                self.assertIn("open_project: opened", logs)
                self.assertEqual(set(outcome.timings), {"parse_s", "match_s", "launch_s", "total_s"})

    def test_the_vocabulary_resolves_a_variant(self) -> None:
        outcome, _ = self.run_voice("abre o VS Code no tarbo quit")
        self.assertEqual((outcome.ok, outcome.text), (True, "A abrir tarvo-kit"))

    def test_a_folder_shortcut_starts_vscode_with_an_argument_list(self) -> None:
        project = self.other / "zefiro"
        project.mkdir()
        write_link(self.other, "zefiro-web", str(project), directory=True)
        self.vscode = lambda: Path(CODE)
        outcome, _ = self.run_voice("abre VS Code no zefiro web")
        self.assertEqual((outcome.ok, outcome.text), (True, "A abrir zefiro-web"))
        self.assertEqual(self.launcher.started, [[CODE, "--new-window", str(project)]])
        self.assertEqual(self.launcher.opened, [])
        self.launcher.started.clear()
        self.vscode = lambda: None
        outcome, _ = self.run_voice("abre VS Code no zefiro web")
        self.assertEqual((outcome.ok, outcome.reason, outcome.state), (False, SC.VSCODE_MISSING, ERROR))
        self.assertEqual(self.launcher.started, [])

    def test_other_targets_are_refused(self) -> None:
        write_link(self.other, "zefiro-tool", "C:\\Windows\\System32\\cmd.exe", arguments="/c echo")
        outcome, _ = self.run_voice("abre VS Code no zefiro tool")
        self.assertEqual((outcome.ok, outcome.reason, outcome.state), (False, SC.REFUSED, VOICE_NONE))
        self.assertIn("recusado", outcome.text)
        self.assertEqual((self.launcher.opened, self.launcher.started), ([], []))

    def test_no_clear_match_opens_nothing_and_shows_up_to_three_names(self) -> None:
        outcome, _ = self.run_voice("abre VS Code no nimbus")
        self.assertEqual((outcome.ok, outcome.state), (False, VOICE_NONE))
        self.assertTrue(outcome.text.startswith("Nenhum projeto com esse nome. Parecidos: "), outcome.text)
        names = outcome.text.split("Parecidos: ", 1)[1].split(", ")
        self.assertLessEqual(len(names), 3)
        self.assertIn("nimbus-deck", names)
        outcome, _ = self.run_voice("abre VS Code no quasar")
        self.assertFalse(outcome.ok)
        self.assertEqual((self.launcher.opened, self.launcher.started), ([], []))

    def test_the_same_name_with_a_different_target_is_ambiguous(self) -> None:
        write_link(self.other, "orla", CODE, arguments='--new-window "C:\\Elsewhere\\orla.code-workspace"')
        outcome, _ = self.run_voice("abre VS Code no orla")
        self.assertEqual((outcome.ok, outcome.reason, outcome.text),
                         (False, SC.AMBIGUOUS, "Não sei qual abrir. Parecidos: orla"))
        self.assertEqual(self.launcher.opened, [])
        # The same target in both folders is one candidate.
        write_link(self.other, "orla", CODE, arguments='--new-window "C:\\Work\\orla.code-workspace"')
        outcome, _ = self.run_voice("abre VS Code no orla")
        self.assertTrue(outcome.ok)

    def test_no_folders_or_no_shortcuts(self) -> None:
        self.folders = []
        outcome, _ = self.run_voice("abre VS Code no orla")
        self.assertEqual((outcome.ok, outcome.reason), (False, SC.NO_SHORTCUTS))
        self.assertIn("shortcut_dirs", outcome.text)

    def test_a_launch_error_is_reported_without_details(self) -> None:
        self.launcher = FakeLauncher(OSError("C:\\Work\\orla secret"))
        outcome, logs = self.run_voice("abre VS Code no orla")
        self.assertEqual((outcome.ok, outcome.reason, outcome.state), (False, SC.LAUNCH_FAILED, ERROR))
        self.assertIn("OSError", logs)

    def test_outcome_repr_carries_no_names(self) -> None:
        outcome, _ = self.run_voice("abre VS Code no orla")
        self.assertNotIn("orla", repr(outcome))
        self.assertNotIn("orla", repr(Intent("open_project", {"project": "orla"})))


def word_tokens(text: str) -> int:
    """An invented token count: one token per word."""
    return len(text.split())


VOCABULARY = Vocabulary(names=(Entry("Velatrix", "name", ("vela trix",)), Entry("Nimbus-Deck", "name", ("nimbos deque",)),
                               Entry("tarvo-kit", "name")),
                        terms=(Entry("kubectl", "term", ("cube control",)),))


class VoiceHintsBuilderTest(unittest.TestCase):
    def test_a_portuguese_command_prompt_with_the_shortcut_names_first(self):
        hints = voice_hints(["orla", "nimbus-deck", "orla_public"], VOCABULARY)
        self.assertIsInstance(hints, SessionHints)
        self.assertEqual(hints.language, "pt")
        # Shortcut names, then the vocabulary names not already listed (same letters: listed once).
        self.assertEqual(hints.prompt, "Abre o VS Code no projeto. Projetos: orla, nimbus-deck, orla_public, Velatrix, "
                                       "tarvo-kit.")
        # Hotwords: shortcut names (split too), then vocabulary names with their variants; no terms.
        self.assertEqual(hints.hotwords, "orla nimbus-deck nimbus deck orla_public orla public Velatrix vela trix "
                                         "nimbos deque tarvo-kit tarvo kit")
        self.assertNotIn("kubectl", hints.hotwords + hints.prompt)
        self.assertNotIn("Vocabulário", hints.prompt)

    def test_without_names_the_prompt_is_the_command_alone(self):
        hints = voice_hints([], Vocabulary())
        self.assertEqual((hints.prompt, hints.hotwords, hints.language), (voice.VOICE_PROMPT, None, "pt"))

    def test_trimming_drops_the_vocabulary_before_the_shortcut_names(self):
        many = [f"projeto{index:02d}-kit" for index in range(60)]
        for tokens in (voice.utf8_tokens, word_tokens):
            hints = voice_hints(many, VOCABULARY, tokens=tokens)
            for part in (hints.prompt, hints.hotwords):
                self.assertLessEqual(len(part), PROMPT_MAX_CHARS)
                self.assertLessEqual(tokens(part), voice.VOICE_PART_MAX_TOKENS)
            self.assertNotIn("Velatrix", hints.prompt)
            self.assertNotIn("Velatrix", hints.hotwords)
            listed = hints.prompt[len(voice.VOICE_PROMPT + voice.PROJECTS_PREFIX):-1].split(", ")
            self.assertEqual(listed, many[:len(listed)])  # in order, cut at the first that does not fit
            self.assertTrue(hints.hotwords.startswith("projeto00-kit projeto00 kit projeto01-kit"))
        # Byte counting never allows more than a real tokenizer would.
        self.assertLess(len(voice_hints(many, tokens=voice.utf8_tokens).prompt),
                        len(voice_hints(many, tokens=word_tokens).prompt))

    def test_a_long_single_name_is_left_out_rather_than_cut(self):
        hints = voice_hints(["x" * 700, "orla"], tokens=word_tokens)
        self.assertEqual(hints.prompt, voice.VOICE_PROMPT)  # the first name does not fit: the list ends there
        self.assertIsNone(hints.hotwords)

    def test_the_parts_fit_the_whisper_prompt_bounds(self):
        self.assertLess(voice.VOICE_PART_MAX_TOKENS, PROMPT_PART_MAX)  # faster-whisper never cuts a part
        ten_seconds = options_for("large-v3-turbo")
        self.assertGreaterEqual(voice.VOICE_ROOM_TOKENS, 10 * ten_seconds.tokens_per_s + ten_seconds.min_new_tokens)


class VoiceHintsProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / "Hub"
        self.folder.mkdir()
        write_link(self.folder, "orla", CODE)

    def test_the_folders_are_listed_at_every_press(self):
        provider = VoiceHints([self.folder], lambda: VOCABULARY, tokens=word_tokens)
        first = provider()
        self.assertIn("orla", first.prompt)
        self.assertNotIn("nimbus-deck", first.prompt)
        write_link(self.folder, "nimbus-deck", CODE)
        second = provider()
        self.assertIn("nimbus-deck", second.prompt)
        self.assertEqual(second, voice_hints(["nimbus-deck", "orla"], VOCABULARY, tokens=word_tokens))

    def test_an_unreadable_folder_gives_the_prompt_without_its_names(self):
        missing = Path(self._tmp.name) / "zefiro"
        with self.assertLogs("quill", logging.INFO) as logs:
            hints = VoiceHints([missing], lambda: Vocabulary())()
        self.assertEqual((hints.prompt, hints.hotwords, hints.language), (voice.VOICE_PROMPT, None, "pt"))
        text = "\n".join(logs.output)
        self.assertIn(SC.FOLDER_MISSING, text)
        for private in (*PRIVATE, str(missing)):
            self.assertNotIn(private, text)

    def test_failures_never_raise_and_log_reason_codes_only(self):
        def lister(_folders):
            raise PermissionError(f"denied: {self.folder}")

        def vocabulary():
            raise ValueError("orla secret")

        def tokens(_text):
            raise RuntimeError("velinor")

        with self.assertLogs("quill.voice", logging.INFO) as logs:
            hints = VoiceHints([self.folder], vocabulary, lister=lister)()
            counted = VoiceHints([self.folder], lambda: VOCABULARY, tokens=tokens)()
        self.assertEqual(hints.prompt, voice.VOICE_PROMPT)
        self.assertEqual(counted, voice_hints(["orla"], VOCABULARY))  # bytes counted instead
        text = "\n".join(logs.output)
        self.assertIn(voice.HINTS_UNLISTED, text)
        self.assertIn("PermissionError", text)
        for private in (*PRIVATE, str(self.folder), "secret"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
