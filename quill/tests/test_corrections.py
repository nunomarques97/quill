"""quill.corrections, the correction key and quill.clipboard.copy_selection, with invented text only."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quill import clipboard, corrections
from quill.clipboard import CF_UNICODETEXT, copy_selection
from quill.config import ConfigError, load_config
from quill.corrections import (
    ACTIVE,
    CONFLICT,
    PENDING,
    CorrectionKey,
    CorrectionStore,
    Corrections,
    Dictation,
    Learner,
    Replacement,
    derive,
    phrase_key,
)
from quill.tests.fakes import OTHER_HWND, TARGET, FakeClock, FakeWin32

DAY = 86_400.0


def pairs(found):
    return [(r.source, r.target) for r in found]


def learned(*observations):
    """Corrections after learning (dictation id, [(source, target), ...]) in order."""
    c = Corrections.empty(0)
    for index, (dictation, items) in enumerate(observations):
        c.learn(dictation, [Replacement(s, t) for s, t in items], float(index))
    return c


def utf16(text):
    return (text + "\x00").encode("utf-16-le")


class DeriveTest(unittest.TestCase):
    def test_single_word_and_phrase_replacements(self):
        self.assertEqual(pairs(derive("corre o kube control agora", "corre o kubectl agora")), [("kube control", "kubectl")])
        self.assertEqual(pairs(derive("abre o velatriz já", "abre o Velatrix já")), [("velatriz", "Velatrix")])
        self.assertEqual(pairs(derive("ram", "run")), [("ram", "run")])  # a one-word dictation can be corrected

    def test_punctuation_only_and_identical_texts_give_nothing(self):
        self.assertEqual(derive("olá, tudo bem", "Olá. Tudo bem!"), [])
        self.assertEqual(derive("igual", "igual"), [])
        self.assertEqual(derive("", "algo"), [])

    def test_sentence_start_capital_is_not_learned_and_target_is_stored_lowercase(self):
        self.assertEqual(derive("olá tudo bem", "Olá tudo bem"), [])
        self.assertEqual(pairs(derive("olá, velatriz aqui", "Olá. Velatrix aqui")), [("velatriz", "velatrix")])
        self.assertEqual(pairs(derive("Ram os testes", "Run os testes")), [("Ram", "run")])
        self.assertEqual(pairs(derive("Ram os testes", "GitHub os testes")), [("Ram", "GitHub")])

    def test_case_only_change_inside_a_sentence_is_learned_unless_case_insensitive(self):
        self.assertEqual(pairs(derive("usa o grafana hoje", "usa o Grafana hoje")), [("grafana", "Grafana")])
        self.assertEqual(derive("usa o grafana hoje", "usa o Grafana hoje", case_sensitive=False), [])

    def test_rewrites_insertions_and_long_phrases_are_not_learned(self):
        self.assertEqual(derive("uma frase qualquer sobre nada", "texto completamente diferente agora sim"), [])
        self.assertEqual(derive("abre o painel", "abre o painel novo"), [])  # pure insertion
        self.assertEqual(derive("abre o painel novo", "abre o painel"), [])  # pure deletion
        self.assertEqual(derive("a b c d e f g h", "x y c z w f q r"), [])  # too little unchanged
        self.assertEqual(len(derive("a b c d e f g h", "x b y d z f w h")), 4)  # half unchanged is a correction
        self.assertEqual(derive("a b c d e f g h i j k l", "x b y d z f w h v j u l"), [])  # six runs
        long = derive("um dois três quatro cinco seis sete oito nove", "um 1 2 3 4 cinco seis sete oito nove")
        self.assertEqual(long, [])  # a side longer than three words

    def test_function_word_swaps_depend_on_the_sentence_and_are_not_learned(self):
        self.assertEqual(derive("o envio dos ficheiros", "o envio do ficheiros"), [])
        self.assertEqual(derive("vi um gato", "vi o gato"), [])
        self.assertEqual(derive("send it to the team", "send it for the team"), [])
        self.assertEqual(pairs(derive("abre de ploy agora", "abre deploy agora")), [("de ploy", "deploy")])
        self.assertEqual(pairs(derive("vou dos ficheiros", "vou ver ficheiros")), [("dos", "ver")])
        self.assertEqual(len(derive("o envio dos ficheiros", "o envio do ficheiros", strict=False)), 1)

    def test_strict_false_lists_every_error(self):
        self.assertEqual(len(derive("a b c d e f g h", "x y c z w f q r", strict=False)), 3)

    def test_partial_selection_is_aligned_inside_the_dictation(self):
        dictated = "primeiro abre o velatriz e depois fecha a janela"
        self.assertEqual(pairs(derive(dictated, "abre o Velatrix", partial=True)), [("velatriz", "Velatrix")])
        self.assertEqual(derive(dictated, "abre o Velatrix"), [])  # as a whole text it is a rewrite

    def test_selection_that_fits_several_places_is_not_located(self):
        # One or two corrected words that share nothing with the dictation fit anywhere.
        self.assertEqual(derive("corre o ram hoje", "run", partial=True), [])
        self.assertEqual(derive("corre o kube control a play agora", "kubectl apply", partial=True), [])
        self.assertEqual(derive("o ram", "run", partial=True), [])
        # An anchor word that appears twice leaves two different places.
        self.assertEqual(derive("o ram o hoje", "o run", partial=True), [])

    def test_selection_located_by_an_anchor_or_the_whole_dictation(self):
        self.assertEqual(pairs(derive("corre o ram hoje", "o run", partial=True)), [("ram", "run")])
        self.assertEqual(pairs(derive("corre o ram hoje", "run hoje", partial=True)), [("ram", "run")])
        self.assertEqual(pairs(derive("ram", "run", partial=True)), [("ram", "run")])
        # The selection covers the whole dictation (and may start before it).
        self.assertEqual(pairs(derive("kube control", "kubectl apply", partial=True)), [("kube control", "kubectl apply")])
        self.assertEqual(pairs(derive("o ram", "abre o run", partial=True)), [("ram", "run")])

    def test_line_breaks_between_corrected_words_become_one_space(self):
        found = derive("abre o velo agora sim já", "abre o Velatrix\nPro agora sim já", partial=True)
        self.assertEqual(pairs(found), [("velo", "Velatrix Pro")])
        self.assertEqual(pairs(derive("abre o velo agora", "abre o Velatrix\t\r\n Pro agora")), [("velo", "Velatrix Pro")])
        self.assertEqual(derive("abre o velo agora", "abre o Velatrix\x00Pro agora"), [])  # never a control character

    def test_replacement_repr_hides_text(self):
        self.assertNotIn("segredo", repr(Replacement("segredo", "outro")))
        self.assertNotIn("segredo", repr(Dictation("d1", "segredo", 1, 0.0)))


class RuleTest(unittest.TestCase):
    def test_active_after_two_distinct_dictations(self):
        c = learned(("d1", [("velatriz", "Velatrix")]))
        self.assertEqual(c.apply("o velatriz"), ("o velatriz", 0))
        self.assertEqual(c.counts()["pending"], 1)
        result = c.learn("d2", [Replacement("velatriz", "Velatrix")], 5.0)
        self.assertEqual((result.counted, result.activated), (1, 1))
        self.assertEqual(c.apply("o velatriz"), ("o Velatrix", 1))

    def test_same_dictation_twice_counts_once(self):
        c = learned(("d1", [("velatriz", "Velatrix")]), ("d1", [("velatriz", "Velatrix")]))
        self.assertEqual(c.entries[0].seen, 1)
        self.assertEqual(c.apply("velatriz")[1], 0)
        c = learned(("d1", [("velatriz", "Velatrix"), ("velatriz", "Velatrix")]))
        self.assertEqual(c.entries[0].seen, 1)

    def test_whole_words_only_and_case_preserving(self):
        c = learned(("d1", [("ram", "run")]), ("d2", [("ram", "run")]))
        self.assertEqual(c.apply("ram, Ram. RAM ramo aram")[0], "run, Run. RUN ramo aram")
        c = learned(("d1", [("pai tone", "Python")]), ("d2", [("pai tone", "Python")]))
        self.assertEqual(c.apply("usa pai tone já")[0], "usa Python já")
        self.assertEqual(c.apply("usa pai, tone já")[0], "usa pai, tone já")  # never across punctuation
        self.assertEqual(c.apply("usa pai  tone")[0], "usa Python")

    def test_longest_phrase_wins(self):
        c = learned(("d1", [("kube", "cube"), ("kube control", "kubectl")]),
                    ("d2", [("kube", "cube"), ("kube control", "kubectl")]))
        self.assertEqual(c.apply("kube control e kube")[0], "kubectl e cube")

    def test_conflicting_targets_are_not_applied(self):
        c = learned(("d1", [("ram", "run")]), ("d2", [("ram", "run")]), ("d3", [("ram", "rum")]))
        self.assertEqual(sorted(c.statuses().values()), [CONFLICT, CONFLICT])
        self.assertEqual(c.apply("ram")[1], 0)

    def test_a_pair_and_its_reverse_conflict(self):
        c = learned(("d1", [("ram", "run")]), ("d2", [("ram", "run")]), ("d3", [("run", "ram")]))
        self.assertEqual(list(c.statuses().values()), [CONFLICT, CONFLICT])
        self.assertEqual(c.apply("ram run"), ("ram run", 0))

    def test_approve_wins_over_conflicts_and_delete_is_remembered(self):
        c = learned(("d1", [("ram", "run")]), ("d2", [("ram", "rum")]))
        c.approve(c.entries[1])
        self.assertEqual(c.statuses(), {0: CONFLICT, 1: ACTIVE})
        self.assertEqual(c.apply("ram")[0], "rum")
        c.approve(c.entries[0])  # approving the other one moves the approval
        self.assertEqual(c.statuses(), {0: ACTIVE, 1: CONFLICT})
        c.delete(c.entries[0])
        self.assertEqual(c.learn("d9", [Replacement("ram", "run")], 9.0).rejected, 1)
        self.assertEqual([e.target for e in c.entries], ["rum"])

    def test_reverse_of_an_approved_pair_is_not_applied(self):
        c = learned(("d1", [("ram", "run")]), ("d2", [("run", "ram")]), ("d3", [("run", "ram")]))
        c.approve(c.entries[0])
        self.assertEqual(c.statuses(), {0: ACTIVE, 1: CONFLICT})
        self.assertEqual(c.apply("ram run")[0], "run run")

    def test_case_only_correction_is_not_its_own_reverse(self):
        c = learned(("d1", [("grafana", "Grafana")]), ("d2", [("grafana", "Grafana")]))
        self.assertEqual(c.apply("usa o grafana")[0], "usa o Grafana")

    def test_invalid_dictation_id_is_refused(self):
        with self.assertRaises(ValueError):
            Corrections.empty(0).learn("../x", [], 0.0)

    def test_entry_count_is_bounded(self):
        c = Corrections.empty(0)
        with mock.patch.object(corrections, "MAX_ENTRIES", 3):
            for index in range(5):
                c.learn(f"d{index}", [Replacement(f"palavra{index}", f"termo{index}")], float(index))
        self.assertEqual([e.source for e in c.entries], ["palavra2", "palavra3", "palavra4"])

    def test_active_and_approved_entries_are_bounded_too_and_pending_go_first(self):
        c = Corrections.empty(0)
        for index in range(4):  # four active entries, one approved
            for seen in ("a", "b"):
                c.learn(f"d{index}{seen}", [Replacement(f"palavra{index}", f"termo{index}")], float(index))
        c.approve(c.entries[1])
        with mock.patch.object(corrections, "MAX_ENTRIES", 3):
            c.learn("d9", [Replacement("outra", "coisa")], 9.0)  # the pending one goes first, then the oldest
            self.assertEqual([e.source for e in c.entries], ["palavra1", "palavra2", "palavra3"])
            again = Corrections.from_json(json.loads(json.dumps(c.to_json())))
        self.assertEqual(len(again.entries), 3)

    def test_unstorable_replacements_are_never_learned(self):
        c = Corrections.empty(0)
        items = [Replacement("velo", "Velatrix\nPro"), Replacement("velo", "a b c d"), Replacement("velo", "x" * 300),
                 Replacement("velo", " ")]
        self.assertEqual(c.learn("d1", items, 0.0).counted, 0)
        self.assertEqual(c.entries, [])

    def test_json_round_trip(self):
        c = learned(("d1", [("ram", "run")]), ("d2", [("ram", "run")]), ("d3", [("kube", "cube")]))
        c.delete(c.entries[1])
        again = Corrections.from_json(json.loads(json.dumps(c.to_json())))
        self.assertEqual(again.to_json(), c.to_json())
        self.assertEqual(again.apply("ram kube")[0], "run kube")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "local" / "corrections.json"
        self.clock = FakeClock()
        self.clock.now = 1_800_000_000.0

    def tearDown(self):
        self.dir.cleanup()

    def store(self):
        return CorrectionStore(self.path, self.clock)

    def test_missing_file_is_empty_and_save_is_atomic(self):
        store = self.store()
        c = store.load()
        self.assertEqual(c.entries, [])
        c.learn("d1", [Replacement("ram", "run")], self.clock())
        self.assertTrue(store.save(c))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["schema"], corrections.SCHEMA)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["corrections.json"])  # no temp file left
        self.assertEqual(self.store().load().entries[0].target, "run")

    def test_failed_write_keeps_the_old_file_and_removes_the_temporary(self):
        store = self.store()
        c = store.load()
        store.save(c)
        before = self.path.read_bytes()
        c.learn("d1", [Replacement("ram", "run")], self.clock())
        with mock.patch.object(corrections.os, "replace", side_effect=PermissionError("denied")):
            self.assertFalse(store.save(c))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["corrections.json"])

    def check_corrupt(self, raw: bytes):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(raw)
        with self.assertLogs("quill.corrections", "WARNING") as logs:
            c = self.store().load()
        self.assertEqual(c.entries, [])
        backups = [p for p in self.path.parent.iterdir() if ".corrupt-" in p.name]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), raw)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["entries"], [])
        self.assertNotIn("segredo", "".join(logs.output))
        return backups[0]

    def test_corrupt_files_are_backed_up_and_replaced(self):
        good = learned(("d1", [("ram", "run")])).to_json()
        cases = [
            b"{not json segredo",
            b"\xff\xfe\x00segredo",
            json.dumps({**good, "schema": 99}).encode(),
            json.dumps({**good, "extra": "segredo"}).encode(),
            json.dumps({**good, "entries": [{"source": "segredo"}]}).encode(),
            json.dumps({**good, "entries": [{**good["entries"][0], "dictations": ["../../x"]}]}).encode(),
            json.dumps({**good, "entries": [{**good["entries"][0], "approved": "yes"}]}).encode(),
            json.dumps({**good, "entries": [{**good["entries"][0], "source": "a b c d e"}]}).encode(),
            json.dumps({**good, "created": "ontem"}).encode(),
            json.dumps({**good, "rejected": [{"source": [], "target": ["x"]}]}).encode(),
            json.dumps([1, 2]).encode(),
            b"[" * 100_000,
            b'{"schema": ' + b"1" * 5000 + b"}",  # over Python's integer conversion limit
            json.dumps({**good, "created": "0001-01-01T00:00:00"}).encode(),  # timestamp() underflows
            json.dumps({**good, "entries": [{**good["entries"][0], "target": "Velatrix\nPro"}]}).encode(),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                backup = self.check_corrupt(raw)
                backup.unlink()

    def test_two_corruptions_in_the_same_second_keep_both_backups(self):
        self.check_corrupt(b"{")
        self.path.write_bytes(b"}")
        self.store().load()
        self.assertEqual(len([p for p in self.path.parent.iterdir() if ".corrupt-" in p.name]), 2)

    def test_unreadable_file_is_never_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{}", encoding="utf-8")
        store = self.store()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")), \
                self.assertLogs("quill.corrections", "ERROR"):
            c = store.load()
        self.assertFalse(store.save(c))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{}")

    def test_stamp_errors_of_the_platform_are_corrupt_files(self):
        good = learned(("d1", [("ram", "run")])).to_json()
        for error in (OSError(22, "invalid"), OverflowError("too big")):
            with self.subTest(error=type(error).__name__), mock.patch.object(corrections, "parse_iso", side_effect=error):
                self.check_corrupt(json.dumps(good).encode()).unlink()

    def test_failed_backup_disables_saving(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"{")
        store = self.store()
        with mock.patch.object(corrections.os, "replace", side_effect=PermissionError("denied")), \
                self.assertLogs("quill.corrections", "ERROR"):
            store.load()
        self.assertFalse(store.writable)
        self.assertEqual(self.path.read_bytes(), b"{")

    def test_saving_resumes_after_a_transient_read_failure(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(learned(("d1", [("ram", "run")])).to_json()), encoding="utf-8")
        store = self.store()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("locked")),                 self.assertLogs("quill.corrections", "ERROR"):
            self.assertEqual(store.load().entries, [])
        self.assertFalse(store.save(Corrections.empty(0.0)))  # never overwrite what was not read
        c = store.load()
        self.assertTrue(store.writable)
        self.assertEqual(len(c.entries), 1)
        self.assertTrue(store.save(c))


class LearnerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "corrections.json"
        self.clock = FakeClock()
        self.clock.now = 1_800_000_000.0

    def tearDown(self):
        self.dir.cleanup()

    def test_learns_saves_and_applies_after_two_dictations(self):
        learner = Learner(CorrectionStore(self.path, self.clock), self.clock)
        with self.assertLogs("quill.corrections", "INFO") as logs:
            learner.learn(Dictation("d1", "abre o velatriz", 1, 0.0), "abre o Velatrix", partial=False, source="test")
            learner.learn(Dictation("d2", "fecha o velatriz", 1, 0.0), "fecha o Velatrix", partial=False, source="test")
            self.assertEqual(learner.apply("o velatriz"), "o Velatrix")
        self.assertNotIn("elatri", "".join(logs.output))
        self.assertEqual(CorrectionStore(self.path, self.clock).load().counts()["active"], 1)

    def test_reloads_when_the_file_changed(self):
        learner = Learner(CorrectionStore(self.path, self.clock), self.clock)
        other = CorrectionStore(self.path, self.clock)
        other.save(learned(("d1", [("ram", "run")]), ("d2", [("ram", "run")])))
        self.assertEqual(learner.apply("ram"), "run")

    def test_multi_line_selection_is_saved_and_reloads(self):
        learner = Learner(CorrectionStore(self.path, self.clock), self.clock)
        dictation = Dictation("d1", "abre o velo agora sim já", 1, 0.0)
        self.assertEqual(learner.learn(dictation, "abre o Velatrix\nPro agora sim já", partial=True, source="test").counted, 1)
        reloaded = CorrectionStore(self.path, self.clock).load()
        self.assertEqual([(e.source, e.target) for e in reloaded.entries], [("velo", "Velatrix Pro")])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["corrections.json"])  # no .corrupt backup

    def test_nothing_derived_is_not_saved(self):
        learner = Learner(CorrectionStore(self.path, self.clock), self.clock)
        result = learner.learn(Dictation("d1", "igual", 1, 0.0), "igual", partial=False, source="test")
        self.assertEqual(result.counted, 0)
        self.assertFalse(self.path.exists())


class CorrectionKeyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.learner = Learner(CorrectionStore(Path(self.dir.name) / "c.json", self.clock), self.clock)
        self.foreground = TARGET.hwnd
        self.selection = "abre o Velatrix"
        self.key = CorrectionKey(self.learner, lambda: self.selection, lambda: self.foreground)

    def tearDown(self):
        self.dir.cleanup()

    def test_paths(self):
        self.assertEqual(self.key.press(), corrections.NO_DICTATION)
        self.key.remember(Dictation("d1", "depois abre o velatriz", TARGET.hwnd, 0.0))
        self.foreground = OTHER_HWND
        self.assertEqual(self.key.press(), corrections.OTHER_WINDOW)
        self.foreground = TARGET.hwnd
        for empty in (None, "", "   ", "x" * 5000):
            self.selection = empty
            self.assertEqual(self.key.press(), corrections.NO_SELECTION)
        self.selection = "depois abre o velatriz"
        self.assertEqual(self.key.press(), corrections.NOTHING_LEARNED)
        self.selection = "abre o Velatrix"
        self.assertEqual(self.key.press(), corrections.LEARNED)
        self.assertEqual(self.learner.corrections.entries[0].source, "velatriz")
        self.assertEqual(self.key.press(), corrections.NOTHING_LEARNED)  # same dictation counts once

    def test_one_corrected_word_without_context_learns_nothing(self):
        self.key.remember(Dictation("d1", "corre o ram hoje", TARGET.hwnd, 0.0))
        self.selection = "run"
        self.assertEqual(self.key.press(), corrections.NOTHING_LEARNED)
        self.key.remember(Dictation("d2", "corre o kube control a play agora", TARGET.hwnd, 0.0))
        self.selection = "kubectl apply"
        self.assertEqual(self.key.press(), corrections.NOTHING_LEARNED)
        self.assertEqual(self.learner.corrections.entries, [])
        self.key.remember(Dictation("d3", "corre o ram hoje", TARGET.hwnd, 0.0))
        self.selection = "o run"
        self.assertEqual(self.key.press(), corrections.LEARNED)
        self.assertEqual([(e.source, e.target) for e in self.learner.corrections.entries], [("ram", "run")])

    def test_clipboard_failure(self):
        def broken():
            raise clipboard.ClipboardError("busy")

        key = CorrectionKey(self.learner, broken, lambda: TARGET.hwnd)
        key.remember(Dictation("d1", "abre o velatriz", TARGET.hwnd, 0.0))
        self.assertEqual(key.press(), corrections.CLIPBOARD_FAILED)


class CopySelectionTest(unittest.TestCase):
    """The clipboard is a fake: nothing here reads or writes the real one."""

    def setUp(self):
        self.api = FakeWin32()
        self.api.clip = [(CF_UNICODETEXT, utf16("do utilizador")), (0xC100, b"rich")]
        self.saved = list(self.api.clip)
        self.clock = FakeClock()

    def copy(self, send_copy, **options):
        return copy_selection(self.api, send_copy, clock=self.clock, sleep=self.clock.sleep, **options)

    def target_copies(self, text):
        def send():
            self.api.clip = [(CF_UNICODETEXT, utf16(text))]
            self.api.sequence += 2
            return True
        return send

    def test_copies_and_restores_every_format(self):
        result = self.copy(self.target_copies("abre o Velatrix"))
        self.assertEqual((result.text, result.reason, result.restored), ("abre o Velatrix", clipboard.COPIED, True))
        self.assertEqual(self.api.clip, self.saved)
        self.assertNotIn("Velatrix", repr(result))

    def test_nothing_selected_times_out_and_restores(self):
        result = self.copy(lambda: True, timeout_s=0.2)
        self.assertEqual((result.text, result.reason, result.restored), (None, clipboard.NO_SELECTION, True))
        self.assertEqual(self.api.clip, self.saved)

    def test_copy_without_text_and_too_long(self):
        def image():
            self.api.clip = [(8, b"dib")]
            self.api.sequence += 1
            return True
        self.assertEqual(self.copy(image).reason, clipboard.NO_TEXT)
        self.assertEqual(self.api.clip, self.saved)
        self.assertEqual(self.copy(self.target_copies("x" * 50), max_chars=10).reason, clipboard.TOO_LONG)
        self.assertEqual(self.api.clip, self.saved)

    def test_send_failure_and_exception_still_restore(self):
        self.assertEqual(self.copy(lambda: False).reason, clipboard.COPY_FAILED)
        self.assertEqual(self.api.clip, self.saved)

        def boom():
            raise RuntimeError("send failed")
        with self.assertRaises(RuntimeError):
            self.copy(boom)
        self.assertEqual(self.api.clip, self.saved)

    def test_change_by_another_program_after_the_copy_is_not_overwritten(self):
        original = self.api.close_clipboard
        closes = []

        def close_clipboard():
            original()
            closes.append(1)
            if len(closes) == 3:  # snapshot, emptying, reading the copy: then another program writes
                self.api.clip = [(CF_UNICODETEXT, utf16("de outro programa"))]
                self.api.sequence += 1

        self.api.close_clipboard = close_clipboard
        with self.assertLogs("quill.clipboard", "WARNING"):
            result = self.copy(self.target_copies("abre"))
        self.assertEqual((result.text, result.restored), ("abre", False))
        self.assertEqual(self.api.clip, [(CF_UNICODETEXT, utf16("de outro programa"))])

    def test_change_while_saving_touches_nothing(self):
        original = self.api.open_clipboard
        opened = []

        def open_clipboard():
            opened.append(1)
            if len(opened) == 2:
                self.api.sequence += 1  # someone wrote between the snapshot and the emptying
            return original()

        self.api.open_clipboard = open_clipboard
        writes = self.api.clipboard_writes
        result = copy_selection(self.api, self.target_copies("abre"), clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual((result.reason, result.restored), (clipboard.BUSY, True))
        self.assertEqual(self.api.clipboard_writes, writes)

    def test_failed_read_after_the_copy_still_restores(self):
        send = self.target_copies("abre o Velatrix")

        def copy_then_hold():
            send()
            self.api.open_failures = 20  # the read of the copy never gets the clipboard
            return True

        result = self.copy(copy_then_hold)
        self.assertEqual((result.text, result.reason, result.restored), (None, clipboard.BUSY, True))
        self.assertEqual(self.api.clip, self.saved)

    def test_busy_clipboard(self):
        self.api.open_failures = 100
        result = self.copy(self.target_copies("abre"))
        self.assertEqual((result.reason, result.restored), (clipboard.BUSY, True))
        self.assertEqual(self.api.clip, self.saved)

    def test_unrestorable_formats_refuse_without_touching(self):
        self.api.clip = [(clipboard.CF_ENHMETAFILE, None)]
        writes = self.api.clipboard_writes
        result = self.copy(self.target_copies("abre"))
        self.assertEqual(result.reason, clipboard.UNSAFE)
        self.assertEqual(self.api.clipboard_writes, writes)

    def test_bitmap_with_a_saved_dib_is_restorable(self):
        self.api.clip = [(clipboard.CF_BITMAP, None), (clipboard.CF_DIB, b"dib")]
        result = self.copy(self.target_copies("abre"))
        self.assertEqual((result.reason, result.restored), (clipboard.COPIED, True))
        self.assertEqual(self.api.clip, [(clipboard.CF_DIB, b"dib")])

    def test_failed_restore_is_reported(self):
        self.api.refuse_set = {0xC100}
        with self.assertLogs("quill.clipboard", "ERROR"):
            result = self.copy(self.target_copies("abre"))
        self.assertFalse(result.restored)

    def test_copy_events_are_ctrl_c(self):
        self.assertEqual(clipboard.copy_events(), [(0x11, 0), (0x43, 0), (0x43, 2), (0x11, 2)])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def load(self, text):
        path = Path(self.dir.name) / "quill.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_default_and_overrides(self):
        config = self.load("")
        self.assertEqual((config.correction_key.name, config.edit_window_s), ("f16", 30))
        self.assertIsNone(self.load('[corrections]\nkey = ""\n').correction_key)
        self.assertEqual(self.load("[corrections]\nedit_window_s = 90\n").edit_window_s, 90)

    def test_a_local_trigger_takes_the_default_key(self):
        config = self.load('[triggers.dictation]\nbuttons = ["xbutton1"]\nkeys = ["f16"]\n')
        self.assertIsNone(config.correction_key)

    def test_invalid_values(self):
        for text in ('[corrections]\nkey = "f13"\n', '[corrections]\nkey = "a"\n', "[corrections]\nkey = 5\n",
                     "[corrections]\nedit_window_s = 1\n", "[corrections]\nedit_window_s = true\n",
                     "[corrections]\nother = 1\n"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load(text)


if __name__ == "__main__":
    unittest.main()
