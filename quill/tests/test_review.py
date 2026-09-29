"""quill.review with a temporary corrections file, scripted answers and invented text."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quill import review
from quill.config import ConfigError
from quill.corrections import ACTIVE, CONFLICT, PENDING, CorrectionStore, Replacement

DAY = 86_400.0
START = 1_800_000_000.0


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


class Answers:
    """Scripted answers to the prompts; EOFError when they run out, like input() at the end of stdin."""

    def __init__(self, *answers, on_ask=None):
        self.answers = list(answers)
        self.questions = []
        self.on_ask = on_ask

    def __call__(self, question):
        self.questions.append(question)
        if self.on_ask is not None:
            self.on_ask(len(self.questions))
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "corrections.json"
        self.clock = Clock()
        self.out = []

    def tearDown(self):
        self.dir.cleanup()

    def store(self):
        return CorrectionStore(self.path, self.clock)

    def seed(self, *observations):
        """Learn (dictation id, [(source, target), ...]) in order and save."""
        store = self.store()
        corrections = store.load()
        for dictation, items in observations:
            corrections.learn(dictation, [Replacement(s, t) for s, t in items], self.clock())
        self.assertTrue(store.save(corrections))
        return corrections

    def review(self, *answers, on_ask=None):
        ask = Answers(*answers, on_ask=on_ask)
        code = review.run_review(self.store(), ask, self.out.append, self.clock)
        return code, ask

    def statuses(self):
        corrections = self.store().load()
        statuses = corrections.statuses()
        return {(e.source, e.target): statuses[i] for i, e in enumerate(corrections.entries)}


class ListingTest(ReviewCase):
    def test_empty(self):
        self.assertEqual(review.listing(self.store().load()), ["Não há correções aprendidas."])

    def test_active_first_then_pending_numbered(self):
        corrections = self.seed(("d1", [("velo", "vélo"), ("ram", "run")]), ("d2", [("ram", "run")]))
        lines = review.listing(corrections)
        self.assertEqual(lines[0], "Correções ativas (aplicadas automaticamente):")
        self.assertEqual(lines[1], "  1. «ram» → «run» (ativa; vista em 2 ditados)")
        self.assertEqual(lines[2], "Correções pendentes (ainda não aplicadas):")
        self.assertEqual(lines[3], "  2. «velo» → «vélo» (pendente; vista em 1 ditado)")

    def test_conflicts_and_approved_labels(self):
        corrections = self.seed(("d1", [("ram", "run")]), ("d2", [("ram", "rum")]))
        self.assertEqual({status for _, status in review.ordered(corrections)}, {CONFLICT})
        self.assertIn("(em conflito;", review.listing(corrections)[1])
        corrections.approve(corrections.entries[1])
        self.assertIn("«ram» → «rum» (aprovada;", review.listing(corrections)[1])


class ReminderTest(ReviewCase):
    def test_not_due_without_corrections(self):
        self.store().save(self.store().load())
        self.clock.now += 30 * DAY
        self.assertIsNone(review.reminder(self.store().load(), self.clock()))

    def test_due_seven_days_after_creation_then_after_the_last_review(self):
        self.seed(("d1", [("ram", "run")]))
        self.clock.now += 7 * DAY - 1
        self.assertIsNone(review.reminder(self.store().load(), self.clock()))
        self.clock.now += 1
        with self.assertLogs("quill.review", "INFO") as logs:
            message = review.reminder(self.store().load(), self.clock())
        self.assertIn("Passaram 7 dias", message)
        self.assertIn("0 ativas, 1 pendentes", message)
        self.assertIn("py -3.12 -m quill.review", message)
        self.assertNotIn("ram", "".join(logs.output))
        self.review()  # opened and closed at once: still counts as a review
        self.assertIsNone(review.reminder(self.store().load(), self.clock()))
        self.clock.now += 8 * DAY
        self.assertIn("Passaram 8 dias", review.reminder(self.store().load(), self.clock()))


class RunReviewTest(ReviewCase):
    def test_approve_delete_keep(self):
        self.seed(("d1", [("ram", "run"), ("velo", "vélo"), ("pai", "pi")]))
        code, ask = self.review("a", "e", "")
        self.assertEqual(code, 0)
        self.assertEqual(len(ask.questions), 3)
        self.assertIn("1/3 «ram» → «run»", ask.questions[0])
        self.assertIn(review.PROMPT, ask.questions[0])
        self.assertEqual(self.statuses(), {("ram", "run"): ACTIVE, ("pai", "pi"): PENDING})
        self.assertEqual(self.out[-1], "Revisão guardada: 1 aprovada, 1 eliminada, 1 mantida.")
        corrections = self.store().load()
        self.assertEqual(corrections.last_review, "2027-01-15T08:00:00+00:00")
        self.assertEqual(corrections.apply("usa o ram"), ("usa o run", 1))

    def test_a_deleted_correction_is_not_learned_again(self):
        self.seed(("d1", [("ram", "run")]))
        self.review("e")
        corrections = self.seed(("d2", [("ram", "run")]), ("d3", [("ram", "run")]))
        self.assertEqual(corrections.entries, [])
        self.assertEqual(corrections.counts()["rejected"], 1)

    def test_approving_one_side_of_a_conflict_makes_it_win(self):
        self.seed(("d1", [("ram", "run")]), ("d2", [("ram", "rum")]))
        self.review("", "a")
        self.assertEqual(self.statuses(), {("ram", "run"): CONFLICT, ("ram", "rum"): ACTIVE})
        self.assertEqual(self.store().load().apply("o ram")[0], "o rum")

    def test_stop_and_end_of_input_keep_the_rest(self):
        self.seed(("d1", [("ram", "run"), ("velo", "vélo")]))
        code, ask = self.review("e", "t")
        self.assertEqual((code, len(ask.questions)), (0, 2))
        self.assertEqual(self.statuses(), {("velo", "vélo"): PENDING})
        self.assertEqual(self.out[-1], "Revisão guardada: 0 aprovadas, 1 eliminada, 1 mantida.")
        code, ask = self.review()  # EOF at the first prompt
        self.assertEqual((code, len(ask.questions)), (0, 1))
        self.assertEqual(self.statuses(), {("velo", "vélo"): PENDING})

    def test_invalid_answers_are_asked_again(self):
        self.seed(("d1", [("ram", "run")]))
        code, ask = self.review("sim", " A ")
        self.assertEqual((code, len(ask.questions)), (0, 2))
        self.assertIn("Resposta inválida: escreva a, e ou t, ou carregue em Enter.", self.out)
        self.assertEqual(self.statuses(), {("ram", "run"): ACTIVE})

    def test_learning_by_the_app_during_the_review_is_kept(self):
        self.seed(("d1", [("ram", "run"), ("velo", "vélo")]))

        def app_learns(asked):
            if asked == 1:
                self.clock.now += 1
                other = self.store()
                corrections = other.load()
                corrections.learn("d9", [Replacement("pai", "pi")], self.clock())
                other.save(corrections)

        self.review("e", "a", on_ask=app_learns)
        self.assertEqual(self.statuses(), {("velo", "vélo"): ACTIVE, ("pai", "pi"): PENDING})

    def test_an_entry_deleted_meanwhile_is_not_recreated(self):
        self.seed(("d1", [("ram", "run")]))

        def app_rewrites(asked):
            self.clock.now += 1
            other = self.store()
            corrections = other.load()
            corrections.entries = []
            other.save(corrections)

        self.review("a", on_ask=app_rewrites)
        self.assertEqual(self.statuses(), {})
        self.assertEqual(self.out[-1], "Revisão guardada: 0 aprovadas, 0 eliminadas, 1 mantida.")

    def test_save_failure_is_reported(self):
        self.seed(("d1", [("ram", "run")]))
        with mock.patch.object(CorrectionStore, "save", return_value=False):
            code, _ = self.review("a")
        self.assertEqual(code, 1)
        self.assertEqual(self.out[-1], "Não foi possível guardar a revisão (ver o registo).")
        self.assertEqual(self.statuses(), {("ram", "run"): PENDING})

    def test_logs_hold_counts_only(self):
        self.seed(("d1", [("segredo", "sigilo")]))
        with self.assertLogs("quill", "INFO") as logs:
            self.review("a")
        text = "".join(logs.output)
        self.assertIn("1 approved, 0 deleted, 0 kept", text)
        self.assertNotIn("segredo", text)
        self.assertNotIn("sigilo", text)


class MainTest(ReviewCase):
    def main(self, *argv, answers=()):
        return review.main(list(argv), ask=Answers(*answers), out=self.out.append, clock=self.clock)

    def test_list_and_check(self):
        self.seed(("d1", [("ram", "run")]))
        self.assertEqual(self.main("--path", str(self.path), "--list"), 0)
        self.assertEqual(self.out[-1], "  1. «ram» → «run» (pendente; vista em 1 ditado)")
        self.assertEqual(self.main("--path", str(self.path), "--check"), 0)
        self.assertEqual(self.out[-1], "A revisão das correções está em dia.")
        self.clock.now += 7 * DAY
        self.main("--path", str(self.path), "--check")
        self.assertTrue(self.out[-1].startswith("Passaram 7 dias"))
        self.assertIsNone(self.store().load().last_review)  # listing and checking are not reviews

    def test_interactive_review(self):
        self.seed(("d1", [("ram", "run")]))
        self.assertEqual(self.main("--path", str(self.path), answers=("a",)), 0)
        self.assertEqual(self.statuses(), {("ram", "run"): ACTIVE})

    def test_corrupt_file_is_backed_up_and_listed_as_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("quill.corrections", "WARNING"):
            self.assertEqual(self.main("--path", str(self.path), "--list"), 0)
        self.assertEqual(self.out[-1], "Não há correções aprendidas.")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["entries"], [])
        self.assertEqual(len(list(self.path.parent.glob("corrections.json.corrupt-*"))), 1)

    def test_default_path_from_the_configuration(self):
        with mock.patch.object(review, "default_path", return_value=self.path):
            self.assertEqual(self.main("--list"), 0)
        self.assertEqual(self.out[-1], "Não há correções aprendidas.")
        with mock.patch.object(review, "default_path", side_effect=ConfigError("bad key")):
            self.assertEqual(self.main("--list"), 2)
        self.assertEqual(self.out[-1], "Configuração inválida: bad key")


if __name__ == "__main__":
    unittest.main()
