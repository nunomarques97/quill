"""Reply pairing tests with invented data: takes paired with local reply files, the replies set and its guide.

Project names, domain terms, reply files and transcriptions are invented; the
recordings are generated tones in a temporary folder that stands in for the
ignored local/ folder, the streamer is a fake engine and the local model a
scripted fake. Claude Code sessions and the Claude config folder are never
read: every test forbids them. No GPU, microphone, sound or Ollama is used.
"""

import json
import os
import re
import stat
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bench import prompts as P
from bench import record
from bench.dataset import load_dataset
from bench.metrics import write_summary
from bench.settings import REPLIES_SCRIPT, SettingsError, load_settings
from bench.tests.test_prompts import (
    PROJECTS,
    TERMS,
    Case,
    FakeStreamer,
    ScriptedModel,
    make_product,
    spoken,
    toml_table,
)
from bench.tests.test_record import ScriptedConsole, samples
from quill import enrich
from quill.config import EXAMPLE_CONFIG, load_config
from quill.reply_terms import ReplyContext

REPO = Path(__file__).resolve().parents[2]
STEPS = REPO / "docs" / "research" / "GRAVAR-RESPOSTAS.md"

SMALL_REPLIES = """# Invented replies script

| id | caso | frase | intenção | projeto | termos | mensagem do Claude | estilo |
|----|------|-------|----------|---------|--------|--------------------|--------|
| rr-01 | opção | Vai pela opção dois, a que só mexe no <termo-1>. | escolher | <projeto-1> | <termo-1> | Oferece opções numeradas. | claude-code |
| rr-02 | parar | Para por aqui e não mexas em mais nada até eu rever. | parar | <projeto-2> | — | Pergunta se avança. | claude-code |
"""
REPLY_PROJECTS = {"<projeto-1>": "nimbus-deck", "<projeto-2>": "orchard"}
REPLY_TERMS = {"<termo-1>": "Kwartz"}
# Invented Claude messages: a question with options and a distinctive identifier, never in any summary.
ASKING = ("Posso ajustar a rotina do Kwartz no ZephyrBoard agora.\n\n1. Mexer só no agendador\n2. Mexer também "
          "no ZephyrBoard\n\nQueres que avance com qual?")
DISTINCT = "ZephyrBoard"


def forbid_claude():
    """Patches that fail a test reading a Claude Code session or the Claude config folder."""
    from quill import claude_reply

    return (mock.patch.object(claude_reply, "last_reply", side_effect=AssertionError("a session was read")),
            mock.patch.object(claude_reply, "claude_config_dir",
                              side_effect=AssertionError("the Claude config folder was read")))


class RepliesCase(Case):
    """The prompts fixture plus a [replies] set (an invented script) and the reply files folder under local/."""

    replies_script = SMALL_REPLIES

    def setUp(self):
        super().setUp()
        self.reply_recordings = self.local / "recordings" / "replies"
        self.replies = self.local / "replies"
        path = self.root / "respostas.md"
        path.write_text(self.replies_script, encoding="utf-8")
        with self.config.open("a", encoding="utf-8") as handle:
            handle.write(f'[replies]\nrecordings_dir = "{self.reply_recordings.as_posix()}"\n'
                         f'recording_script = "{path.as_posix()}"\nexpected_takes = 2\n'
                         + '[replies.projects]\n' + toml_table(REPLY_PROJECTS)
                         + '[replies.terms]\n' + toml_table(REPLY_TERMS))
        self.claude = [patcher.start() for patcher in forbid_claude()]
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for forbidden in self.claude:
            forbidden.assert_not_called()

    def reply_file(self, take_id, text):
        self.replies.mkdir(parents=True, exist_ok=True)
        (self.replies / f"{take_id}.md").write_text(text, encoding="utf-8")

    def reply_set(self):
        return load_settings(self.config).for_set("replies")

    def record_replies(self, take_ids=None):
        chosen = self.reply_set()
        rows = [row for row in record.load_script(chosen) if take_ids is None or row.id in take_ids]
        session = record.Session(rows=rows, mapping=dict(chosen.projects), known=frozenset(REPLY_PROJECTS.values()),
                                 recordings_dir=chosen.recordings_dir, manifest_path=chosen.manifest,
                                 capture_factory=None, console=ScriptedConsole([]),
                                 now=lambda: datetime(2026, 1, 2, 3, 4, 5), terms=dict(chosen.terms))
        for row in rows:
            session.save(row, samples(1.2), 1.2)

    def texts(self):
        prompts = {row.id: spoken(row) for row in self.rows()}
        replies = {row.id: P._fill(row.text, {**REPLY_PROJECTS, **REPLY_TERMS})
                   for row in P.load_reply_rows(self.reply_set())}
        return {**prompts, **replies}

    def run_main(self, *args, model=None, reply_on=True, edit=None):
        model = model or ScriptedModel()
        streamer = FakeStreamer(self.texts())
        asked = []  # the reply lookups the hints were given, called as the app would

        def factory(vocab, generic):
            product = make_product(self.root, model)
            if reply_on:
                product.reply_settings = load_config(None, EXAMPLE_CONFIG).claude_code
            hints_for = product.hints_for

            def spy(info, reply=None):
                if reply is not None:
                    asked.append(reply)
                return hints_for(info, reply=reply) if reply is not None else hints_for(info)

            product.hints_for = spy
            if edit is not None:
                edit(product)
            return product

        lines = []
        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        with mock.patch("bench.audio_mme.WinMM", side_effect=AssertionError("the microphone was opened")):
            code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                           *args], product_factory=factory, streamer_factory=streamer.factory,
                          results_dir=self.results, out=lines.append)
        return SimpleNamespace(code=code, lines=lines, summary=summary, model=model, asked=asked)

    def corrections(self, model):
        return [call for call in model.calls if not call.enrich]


# ---------------------------------------------------------------- pairing through the app's path


class PairingTest(RepliesCase):
    def test_a_paired_take_gets_its_reply_in_hints_correction_and_enrichment(self):
        self.record()
        self.reply_file("pp-01", ASKING)
        run = self.run_main("--set", "prompts")
        self.assertEqual(run.code, 0, run.lines)
        # Hints: only the paired take is given the reply lookup, which reads its file (never a session).
        self.assertTrue(run.asked)
        context = run.asked[0](self.root / "repos" / "nimbus-deck")
        self.assertIsInstance(context, ReplyContext)
        self.assertIn("Kwartz", context.terms)
        self.assertTrue(context.asks)
        self.assertIsNone(run.asked[0](self.root / "repos" / "orchard"))  # another folder: no reply
        # Correction: the reply terms reach only the paired take's prompt, as data.
        with_terms = [call for call in self.corrections(run.model) if "<reply_terms>" in call.user]
        self.assertTrue(with_terms)
        self.assertTrue(all("rotina" in call.user for call in with_terms))  # pp-01's dictation only
        block = json.loads(run.summary.read_text(encoding="utf-8"))["sets"]["prompts"]["reply_context"]
        self.assertEqual((block["enabled"], block["paired"], block["unpaired"], block["pending_pairs"]),
                         (True, 1, 2, 0))
        self.assertEqual(block["lookups"], {"ok": 1})
        self.assertEqual(block["invalid_pairs"], {})
        with_reply, without = block["with_reply"], block["without_reply"]
        self.assertEqual((with_reply["takes"], with_reply["replies_used"]), (1, 1))
        self.assertEqual((without["takes"], without["replies_used"]), (1, 0))
        # Enrichment: a short answer to a question is typed as corrected (skipped as a reply), not restructured.
        self.assertEqual(with_reply["enrichment"], {"asked": 0, "enriched": 0, "skipped_reply": 1})
        self.assertEqual(without["enrichment"], {"asked": 1, "enriched": 1, "skipped_reply": 0})
        for view in (with_reply, without):
            self.assertEqual((view["lost"], view["invented"], view["invented_from_reply"]), (0, 0, 0))
            self.assertEqual(view["term_errors"], 0)
            self.assertIsNotNone(view["latency"]["release_p95_s"])
            self.assertIsNotNone(view["wer"])
        self.assertIsNotNone(with_reply["latency"]["lookup_p95_s"])
        self.assertIsNone(without["latency"]["lookup_p95_s"])
        report = "\n".join(run.lines)
        self.assertIn("reply pairs (prompts): 1 paired, 2 unpaired; reply context on", report)
        self.assertIn("  with the reply: WER ", report)
        self.assertIn("  without the reply: WER ", report)
        summary = json.loads(run.summary.read_text(encoding="utf-8"))
        self.assertTrue(summary["rewrite"]["last_reply_context"])
        # Counts only: no reply word in the summary or the report.
        serialized = run.summary.read_text(encoding="utf-8")
        for secret in (DISTINCT, "agendador", "Kwartz"):
            self.assertNotIn(secret.casefold(), serialized.casefold())
            self.assertNotIn(secret.casefold(), report.casefold())
        # The private rows (ignored results/) say which take used a reply.
        result = next((self.results / "prompts").iterdir())
        takes = {t["id"]: t for t in json.loads((result / "takes.json").read_text(encoding="utf-8"))}
        self.assertEqual((takes["pp-01"]["reply"], takes["pp-01"]["reply_used"]), ("ok", True))
        self.assertEqual(takes["pp-01"]["enrichment"], enrich.REPLY)
        self.assertEqual((takes["pp-02"]["reply"], takes["pp-02"]["reply_used"]), ("", False))
        self.assertTrue((result / "takes-no-reply.json").is_file())

    def test_no_reply_context_switch_uses_no_reply(self):
        self.record()
        self.reply_file("pp-01", ASKING)
        run = self.run_main("--set", "prompts", "--no-reply-context")
        self.assertEqual(run.code, 0, run.lines)
        self.assertEqual(run.asked, [])
        self.assertFalse(any("<reply_terms>" in call.user for call in self.corrections(run.model)))
        summary = json.loads(run.summary.read_text(encoding="utf-8"))
        block = summary["sets"]["prompts"]["reply_context"]
        self.assertEqual((block["enabled"], block["paired"], block["unpaired"]), (False, 1, 2))
        self.assertIsNone(block["with_reply"])
        self.assertEqual(block["without_reply"]["takes"], 1)
        self.assertEqual(block["lookups"], {})
        self.assertFalse(summary["rewrite"]["last_reply_context"])

    def test_the_app_config_off_uses_no_reply(self):
        self.record()
        self.reply_file("pp-01", ASKING)
        run = self.run_main("--set", "prompts", reply_on=False)
        self.assertEqual(run.code, 0, run.lines)
        self.assertEqual(run.asked, [])
        self.assertFalse(any("<reply_terms>" in call.user for call in self.corrections(run.model)))
        block = json.loads(run.summary.read_text(encoding="utf-8"))["sets"]["prompts"]["reply_context"]
        self.assertFalse(block["enabled"])

    def test_without_reply_files_every_take_is_unpaired_and_unchanged(self):
        self.record()
        run = self.run_main("--set", "prompts")
        self.assertEqual(run.code, 0, run.lines)
        self.assertEqual(run.asked, [])
        self.assertFalse(any("<reply_terms>" in call.user for call in self.corrections(run.model)))
        block = json.loads(run.summary.read_text(encoding="utf-8"))["sets"]["prompts"]["reply_context"]
        self.assertEqual((block["paired"], block["unpaired"], block["with_reply"], block["without_reply"]),
                         (0, 3, None, None))

    def test_the_replies_set_answers_the_window_of_its_row(self):
        self.record_replies()
        self.reply_file("rr-01", ASKING)
        run = self.run_main("--set", "replies")
        self.assertEqual(run.code, 0, run.lines)
        block = json.loads(run.summary.read_text(encoding="utf-8"))["sets"]["replies"]
        self.assertEqual(block["status"], "measured")
        # The answers say no project name: the window is the row's projeto, so each finds its folder.
        self.assertEqual(block["projects_detected"], 2)
        reply = block["reply_context"]
        self.assertEqual((reply["paired"], reply["unpaired"], reply["pending_pairs"]), (1, 1, 1))
        self.assertEqual(reply["with_reply"]["replies_used"], 1)
        self.assertEqual(reply["with_reply"]["term_occurrences"], 1)

    def test_a_summary_leaking_a_reply_word_is_refused(self):
        self.record()
        self.reply_file("pp-01", ASKING)

        def leak(product):
            product.info["keep_alive"] = f"x {DISTINCT}"  # a reply word in an aggregate field

        run = self.run_main("--set", "prompts", edit=leak)
        self.assertEqual(run.code, 1)
        self.assertFalse(run.summary.exists())
        self.assertIn("not written", run.lines[-1])

    def test_a_reply_naming_the_engine_model_still_writes_the_summary(self):
        self.record()
        self.reply_file("pp-01", "Corre com o fake-engine e o fake-model no ZephyrBoard? Queres?")
        run = self.run_main("--set", "prompts")
        self.assertEqual(run.code, 0, run.lines)
        self.assertTrue(run.summary.is_file())


# ---------------------------------------------------------------- reply files


class ReplyFileTest(RepliesCase):
    def test_reads_a_plain_file_and_reports_a_missing_pair(self):
        self.replies.mkdir(parents=True)
        (self.replies / "pp-01.md").write_bytes(bytes.fromhex("efbbbf") + "Queres?\r\nSim.\r\n".encode())
        found = P.read_reply_file(self.replies, "pp-01")
        self.assertEqual((found.reason, found.text), (P.PAIR_OK, "Queres?\nSim."))
        self.assertNotIn("Queres", repr(found))
        self.assertEqual(P.read_reply_file(self.replies, "pp-02").reason, P.PAIR_MISSING)
        self.assertEqual(P.load_pairs(None, ["pp-01"]), {"pp-01": P.ReplyFile(P.PAIR_MISSING)})
        pairs = P.load_pairs(self.replies, ["pp-01", "pp-02", "pp-01"])
        self.assertEqual({k: v.reason for k, v in pairs.items()}, {"pp-01": P.PAIR_OK, "pp-02": P.PAIR_MISSING})
        self.assertEqual(P.reply_file_ids(self.replies), ["pp-01"])

    def test_refuses_bad_ids_links_folders_large_empty_and_undecodable_files(self):
        self.reply_file("pp-01", "ok")
        for bad in ("../pp-01", "pp-1", "PP-01", "pp-01/x"):
            self.assertEqual(P.read_reply_file(self.replies, bad).reason, P.PAIR_MISSING, bad)
        (self.replies / "pp-02.md").mkdir()
        self.assertEqual(P.read_reply_file(self.replies, "pp-02").reason, P.PAIR_NOT_REGULAR)
        (self.replies / "pp-03.md").write_bytes(b"x" * (P.MAX_REPLY_BYTES + 1))
        self.assertEqual(P.read_reply_file(self.replies, "pp-03").reason, P.PAIR_TOO_LARGE)
        (self.replies / "pp-04.md").write_text("  \n", encoding="utf-8")
        self.assertEqual(P.read_reply_file(self.replies, "pp-04").reason, P.PAIR_EMPTY)
        (self.replies / "pp-05.md").write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(P.read_reply_file(self.replies, "pp-05").reason, P.PAIR_UNREADABLE)

        real = os.lstat

        def linked(path, attributes=0, mode=None):
            def fake(name):
                info = real(name)
                if not name.endswith("pp-01.md"):
                    return info
                values = list(info)
                values[0] = mode if mode is not None else info.st_mode
                result = os.stat_result(values)
                fields = {key: getattr(result, key) for key in dir(result) if key.startswith("st_")}
                return SimpleNamespace(**{**fields, "st_file_attributes": attributes}) if attributes else result
            return fake

        self.assertEqual(P.read_reply_file(self.replies, "pp-01", lstat=linked(None, mode=stat.S_IFLNK | 0o644)).reason,
                         P.PAIR_LINKED)
        self.assertEqual(P.read_reply_file(self.replies, "pp-01",
                                           lstat=linked(None, attributes=P.FILE_ATTRIBUTE_REPARSE_POINT)).reason,
                         P.PAIR_LINKED)

    def test_a_folder_outside_local_or_linked_pairs_nothing(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "pp-01.md").write_text("Queres?", encoding="utf-8")
        self.assertEqual(P.read_reply_file(outside, "pp-01").reason, P.PAIR_OUTSIDE)
        self.reply_file("pp-01", "Queres?")
        real = os.lstat

        def reparse(name):
            info = real(name)
            if Path(name) != self.replies:
                return info
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=P.FILE_ATTRIBUTE_REPARSE_POINT)

        self.assertEqual(P.read_reply_file(self.replies, "pp-01", lstat=reparse).reason, P.PAIR_OUTSIDE)

    def test_replies_dir_must_stay_under_local(self):
        self.assertEqual(load_settings(self.config).replies_dir, self.replies)
        with self.config.open("a", encoding="utf-8") as handle:
            handle.write(f'replies_dir = "{(self.root / "elsewhere").as_posix()}"\n')
        text = self.config.read_text(encoding="utf-8")
        # Move the key into the [replies] table (appended after its subtables above).
        text = text.replace(f'replies_dir = "{(self.root / "elsewhere").as_posix()}"\n', "")
        text = text.replace("[replies]\n", f'[replies]\nreplies_dir = "{(self.root / "elsewhere").as_posix()}"\n')
        self.config.write_text(text, encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.config)


class PairingUnitTest(unittest.TestCase):
    def setUp(self):
        self.settings = load_config(None, EXAMPLE_CONFIG).claude_code
        self.claude = [patcher.start() for patcher in forbid_claude()]
        self.addCleanup(mock.patch.stopall)

    def test_one_lookup_per_take_owned_by_its_folder(self):
        ticks = iter([1.0, 1.25, 9.0])
        pairing = P.Pairing(self.settings, {"pp-01": ASKING}, clock=lambda: next(ticks))
        folder = Path("repos/nimbus-deck")
        first = pairing.lookup("pp-01", folder)
        self.assertIsInstance(first, ReplyContext)
        self.assertIs(pairing.lookup("pp-01", folder), first)  # looked up once, as once per hold
        self.assertIsNone(pairing.lookup("pp-01", Path("repos/orchard")))
        self.assertIsNone(pairing.lookup("pp-02", folder))
        self.assertIsNone(pairing.lookup("pp-01", None))
        self.assertEqual(pairing.outcome("pp-01"), ("ok", 0.25))
        self.assertEqual(pairing.outcome("pp-02"), ("", 0.0))
        with P.without_reply(pairing):
            self.assertIsNone(pairing.lookup("pp-01", folder))
        self.assertIs(pairing.lookup("pp-01", folder), first)
        self.assertIn(DISTINCT, pairing.terms())
        self.assertNotIn("Posso", repr(pairing))
        for forbidden in self.claude:
            forbidden.assert_not_called()

    def test_the_caps_of_the_app_apply(self):
        small = replace(self.settings, last_reply_max_chars=200, last_reply_max_terms=1)
        pairing = P.Pairing(small, {"pp-01": "palavra " * 200 + "Queres o ZephyrBoard?"})
        context = pairing.lookup("pp-01", Path("repos/x"))
        self.assertLessEqual(len(context.terms), 1)

    def test_invented_words_from_the_reply(self):
        pairing = P.Pairing(self.settings, {"pp-01": "Usa o ZephyrBoard ou o agendador?"})
        words = pairing.words("pp-01")
        self.assertEqual(P.invented_from_reply("sim, usa o agendador", "sim usa o agendador",
                                               "Sim, usa o agendador e o ZephyrBoard.", words), 1)
        self.assertEqual(P.invented_from_reply("sim, usa o agendador", "sim usa o agendador",
                                               "Sim, usa o agendador.", words), 0)


# ---------------------------------------------------------------- --check


class CheckTest(RepliesCase):
    def test_pending_pairs_are_reported_without_failing(self):
        self.record()
        self.record_replies()
        self.reply_file("rr-01", ASKING)
        run = self.run_main()
        self.assertEqual(run.code, 0, run.lines)
        replies = json.loads(run.summary.read_text(encoding="utf-8"))["sets"]["replies"]["reply_context"]
        self.assertEqual(replies["pending_pairs"], 1)
        lines = []
        code = P.main(["--check", str(run.summary)], out=lines.append)
        report = "\n".join(lines)
        self.assertIn("reply pairs (replies): 1 paired, 1 unpaired; reply context on", report)
        self.assertIn("pending pairs (replies): 1", report)
        data = json.loads(run.summary.read_text(encoding="utf-8"))
        data["sets"]["replies"]["reply_context"]["pending_pairs"] = 0
        complete = self.results / "complete.json"
        complete.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(code, P.main(["--check", str(complete)], out=lambda line: None))  # pending is not a failure
        self.assertNotEqual(code, 2)

    def test_a_malformed_block_fails_the_check(self):
        self.record()
        run = self.run_main("--set", "prompts")
        data = json.loads(run.summary.read_text(encoding="utf-8"))
        data["sets"]["prompts"]["reply_context"]["paired"] = -1
        broken = self.results / "broken.json"
        broken.write_text(json.dumps(data), encoding="utf-8")
        lines = []
        self.assertEqual(P.main(["--check", str(broken)], out=lines.append), 2)
        self.assertIn("error: prompts: reply_context malformed (paired)", lines)

    def test_reply_problems(self):
        self.assertEqual(P.reply_problems("x"), ["not an object"])
        block = P.reply_context_block(True, ["pp-01"], {"pp-01": P.ReplyFile(P.PAIR_LINKED)})
        self.assertEqual(P.reply_problems(block), [])
        self.assertEqual(block["invalid_pairs"], {P.PAIR_LINKED: 1})
        block["paired"] = 1
        self.assertIn("without_reply", P.reply_problems(block))


class PrivacyTest(unittest.TestCase):
    def test_write_summary_refuses_a_reply_word(self):
        import tempfile

        pairing = P.Pairing(load_config(None, EXAMPLE_CONFIG).claude_code, {"pp-01": ASKING})
        names = P.refused_project_words(P.reply_words(pairing))
        self.assertIn(DISTINCT, names)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                write_summary(Path(tmp) / "s.json", {"note": f"o {DISTINCT}"}, list(pairing.texts.values()), names)
            with self.assertRaises(ValueError):  # a run of the reply's words is refused as text
                write_summary(Path(tmp) / "s.json", {"note": "mexer também no agendador"},
                              list(pairing.texts.values()), [])
            write_summary(Path(tmp) / "s.json", {"paired": 1}, list(pairing.texts.values()), names)


# ---------------------------------------------------------------- the replies script and its recording


class ScriptTest(unittest.TestCase):
    def test_committed_script_shape_and_placeholders(self):
        text = REPLIES_SCRIPT.read_text(encoding="utf-8")
        rows = P.parse_reply_script(text, "rr")
        self.assertEqual([row.id for row in rows], [f"rr-{n:02d}" for n in range(1, 11)])
        self.assertEqual({row.case for row in rows}, set(P.REPLY_CASES))
        self.assertEqual({row.project for row in rows}, {"<projeto-1>", "<projeto-2>"})
        terms = [term for row in rows for term in row.terms]
        self.assertEqual(sorted(set(terms), key=lambda t: int(t[7:-1])), [f"<termo-{n}>" for n in range(1, 9)])
        for row in rows:
            self.assertTrue(row.message)
            self.assertNotIn("<projeto-", row.text)  # an answer does not say the project
            self.assertLessEqual(len(row.text.split()), 25)  # short answers
            # Only placeholders name domain terms: no capitalised word inside a phrase but its first.
            self.assertEqual([w for w in re.findall(r"\b\w+\b", row.text)[1:] if w[0].isupper()], [], row.id)
        self.assertNotIn("\\Users\\", text)
        for column in ("| mensagem do Claude |", "| termos |"):
            self.assertIn(column, text)

    def test_invalid_rows(self):
        header = ("| id | caso | frase | intenção | projeto | termos | mensagem do Claude | estilo |\n"
                  "|---|---|---|---|---|---|---|---|\n")
        good = "| rr-01 | parar | Para aqui. | parar | <projeto-1> | — | Pergunta se avança. | claude-code |\n"
        self.assertEqual(len(P.parse_reply_script(header + good)), 1)
        bad = {
            "caso": good.replace("| parar | Para", "| outro | Para"),
            "estilo": good.replace("claude-code", "default"),
            "projeto": good.replace("<projeto-1>", "nimbus"),
            "termos": good.replace("Para aqui.", "Para no <termo-1>."),
            "mensagem": good.replace("Pergunta se avança.", "—"),
            "outro projeto": good.replace("Para aqui.", "Para no <projeto-2>."),
        }
        for name, row in bad.items():
            with self.assertRaises(P.PromptScriptError, msg=name):
                P.parse_reply_script(header + row)


class RecordTest(RepliesCase):
    def dry_run(self, *args):
        console = ScriptedConsole([])
        with mock.patch.object(record, "WinMM", side_effect=AssertionError("the microphone was opened")):
            code = record.main(["--config", str(self.config), "--set", "replies", "--dry-run", *args],
                               console=console)
        return code, "\n".join(console.lines)

    def test_dry_run_counts_only(self):
        code, shown = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("replies: 2 script rows, 0 recorded, 2 pending (dry run: the microphone is not opened)", shown)
        self.assertIn("local mapping: 2 of 2 projects and 1 of 1 terms named", shown)
        self.assertIn("reply files: 0 of 2 saved, 0 recorded takes paired, pending pairs 2", shown)
        self.record_replies(["rr-01"])
        self.reply_file("rr-01", ASKING)
        self.reply_file("rr-02", "Avanço?")
        code, shown = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("replies: 2 script rows, 1 recorded, 1 pending", shown)
        self.assertIn("reply files: 2 of 2 saved, 1 recorded takes paired, pending pairs 1", shown)
        for secret in ("Kwartz", "nimbus", "orchard", "opção dois", DISTINCT, "Oferece"):
            self.assertNotIn(secret, shown)

    def test_dry_run_of_the_committed_script_with_the_default_config_paths(self):
        text = self.config.read_text(encoding="utf-8")
        text = re.sub(r'recording_script = "[^"]*respostas\.md"\nexpected_takes = 2\n', "", text)
        self.config.write_text(text, encoding="utf-8")
        code, shown = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("replies: 10 script rows, 0 recorded, 10 pending", shown)
        self.assertIn("local mapping: 2 of 2 projects and 1 of 8 terms named", shown)

    def test_records_an_answer_with_the_claude_message_description_as_context(self):
        from bench.audio_mme import Capture
        from bench.tests.test_record import FakeWaveIn

        fake = FakeWaveIn()

        def answer():
            for _ in range(30):
                fake.advance(0.05)
                self.capture.pump()
            return ""

        def capture(api, index):
            self.capture = Capture(fake, 0, background=False, clock=fake.clock)
            return self.capture

        console = ScriptedConsole(["", answer, "q"])
        with mock.patch.object(record, "Capture", side_effect=capture):
            code = record.main(["--config", str(self.config), "--set", "replies"], api=fake, console=console)
        self.assertEqual(code, 0)
        shown = "\n".join(console.lines)
        self.assertIn("Mensagem do Claude a que respondes (não ler): Oferece opções numeradas.", shown)
        self.assertIn("Resposta a dizer: Vai pela opção dois, a que só mexe no Kwartz.", shown)
        manifest = json.loads(self.reply_set().manifest.read_text(encoding="utf-8"))
        entry = manifest["gravacoes"]["rr-01"]
        self.assertEqual((entry["projetos"], entry["termos"]), (REPLY_PROJECTS, REPLY_TERMS))
        dataset = load_dataset(self.reply_set())
        self.assertEqual(([t.id for t in dataset.takes], dataset.pending), (["rr-01"], ("rr-02",)))
        self.assertEqual(dataset.takes[0].terms, ("Kwartz",))
        self.assertEqual(fake.opened, fake.closed)

    def test_missing_terms_stop_before_the_microphone(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('"<termo-1>" = "Kwartz"\n', ""),
                               encoding="utf-8")
        console = ScriptedConsole([])
        with mock.patch.object(record, "WinMM", side_effect=AssertionError("the microphone was opened")):
            code = record.main(["--config", str(self.config), "--set", "replies"], console=console)
        self.assertEqual(code, 2)
        self.assertIn("[replies.terms]", console.lines[-1])


class StepsTest(unittest.TestCase):
    def test_recording_steps_are_numbered_and_name_the_commands(self):
        text = STEPS.read_text(encoding="utf-8")
        steps = re.findall(r"^(\d+)\. ", text, re.MULTILINE)
        self.assertGreaterEqual(len(steps), 5)
        self.assertEqual(steps, [str(n) for n in range(1, len(steps) + 1)])
        for needle in ("local/replies/rr-01.md", "[replies.terms]", "[replies.projects]", "local/bench.toml",
                       "py -3.12 -m bench.record --set replies", "py -3.12 -m bench.record --set replies --dry-run",
                       ".venv\\Scripts\\python -m bench.prompts --summary docs/research/replies-summary.json",
                       "bench/results/", "local/"):
            self.assertIn(needle, text)
        # The measurement needs the transcription stack, which only the .venv interpreter has.
        self.assertNotIn("py -3.12 -m bench.prompts", text)
        self.assertNotIn("\\Users\\", text)
        self.assertNotIn("Kwartz", text)


if __name__ == "__main__":
    unittest.main()
