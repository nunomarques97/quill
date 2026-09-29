"""quill.vocabulary tests with invented names and terms only."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from quill import vocabulary as vocab
from quill.vocabulary import Matcher, VocabularyError, fold, hint_list, hints_within_limit, parse_vocabulary

GENERIC = ["commit", "deploy", "workflow", "pull request", "dashboard"]


def personal(**data):
    base = {"names": ["Nimbus-Deck", "Velatrix", "tarvo-kit"], "terms": ["kubectl", "Grafana"],
            "variants": {"kubectl": ["cube control"], "Nimbus-Deck": ["nimbos deque"]}}
    return parse_vocabulary({**base, **data})


class FoldTest(unittest.TestCase):
    def test_accents_case_separators_and_spellings(self):
        self.assertEqual(fold("Nimbus-Deck"), fold("nimbus deck"))
        self.assertEqual(fold("Crípto Rádar"), fold("krypto-radar"))  # accents, k/c, y/i
        self.assertEqual(fold("Phalanx"), fold("falanx"))
        self.assertEqual(fold("tarvo kit"), fold("tarvokitt"))  # doubled letters collapse
        self.assertEqual(fold("¿?"), "")

    def test_distance_is_bounded(self):
        self.assertEqual(vocab.distance("velatrix", "velatriz", 1), 1)
        self.assertEqual(vocab.distance("velatrix", "vealtrix", 1), 1)  # a transposition is one edit
        self.assertEqual(vocab.distance("velatrix", "vela", 2), 3)
        self.assertEqual(vocab.distance("abc", "xyz", 1), 2)

    def test_edit_bound_grows_with_length(self):
        self.assertEqual([vocab.max_edits(n) for n in (3, 6, 7, 11, 12)], [0, 0, 1, 1, 2])


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text):
        path = self.root / "vocabulary.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_is_empty(self):
        self.assertIs(vocab.load_vocabulary(self.root / "none.toml"), vocab.EMPTY)

    def test_example_file_is_valid_and_invented(self):
        loaded = vocab.load_vocabulary(vocab.EXAMPLE_VOCABULARY)
        self.assertEqual(loaded.counts(), {"names": 3, "terms": 3, "variants": 4})
        self.assertEqual(loaded.names[0].text, "Nimbus-Deck")
        self.assertEqual(loaded.names[0].variants, ("nimbos deque", "nimbo deck"))

    def test_entries_keep_file_order_and_kind(self):
        loaded = vocab.load_vocabulary(self.write('names = ["Zulo", "Anta-Bar"]\nterms = [" kubectl "]\n'))
        self.assertEqual([(e.text, e.kind) for e in loaded.entries], [("Zulo", "name"), ("Anta-Bar", "name"), ("kubectl", "term")])

    def assert_invalid(self, text, message):
        with self.assertRaises(VocabularyError) as caught:
            vocab.load_vocabulary(self.write(text))
        self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    def test_invalid_files_name_the_field_never_the_value(self):
        self.assert_invalid('names = "Zulo"\n', "names must be a list")
        self.assert_invalid('names = [3]\n', "names[0] must be a string")
        self.assert_invalid('names = ["--"]\n', "names[0] must have letters")
        self.assert_invalid(f'terms = ["{"x" * 61}"]\n', "terms[0] must be at most 60")
        self.assert_invalid('terms = ["a b c d e"]\n', "at most 4 words")
        self.assert_invalid('extra = 1\n', "unknown field extra")
        self.assert_invalid('variants = 1\n', "variants must be a table")
        message = self.assert_invalid('names = ["Zulo"]\n[variants]\n"Secretname" = ["x"]\n', "variants entry 1 is not a listed")
        self.assertNotIn("Secretname", message)
        self.assert_invalid('names = ["Zulo", "zulo"]\n', "names[1] repeats names[0]")
        self.assert_invalid('names = ["Zulo"]\nterms = ["Anta"]\n[variants]\n"Anta" = ["zu lo"]\n', "repeats names[0]")
        message = self.assert_invalid('names = ["Zulo"\n', "is not valid TOML")
        self.assertNotIn("Zulo", message)

    def test_size_limits(self):
        self.assert_invalid("names = [" + ", ".join(f'"n{i}"' for i in range(501)) + "]\n", "more than 500")
        self.assert_invalid('names = ["Zulo"]\n[variants]\n"Zulo" = [' + ", ".join(f'"v{i}"' for i in range(21)) + "]\n", "more than 20")

    def test_cli_check_prints_counts_only(self):
        path = self.write('names = ["Zulo-Privado"]\nterms = ["kubectl"]\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(vocab.main(["--check", str(path)]), 0)
        self.assertIn("1 names, 1 terms, 0 variants", out.getvalue())
        self.assertNotIn("Zulo", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(vocab.main(["--check", str(self.root / "none.toml")]), 2)
            self.assertEqual(vocab.main(["--check", str(self.write("names = 1\n"))]), 1)
            self.assertEqual(vocab.main([]), 2)


class HintTest(unittest.TestCase):
    def test_priority_order_and_duplicates(self):
        loaded = personal()
        words = hint_list(loaded, ["zeta-board", "velatrix"], ["grafana", "commit"])
        self.assertEqual(words, ["Nimbus-Deck", "Velatrix", "tarvo-kit", "zeta-board", "kubectl", "Grafana", "commit"])
        self.assertNotIn("nimbos deque", words)  # variants are never hints

    def test_generic_terms_file_is_read(self):
        terms = vocab.load_generic_terms()
        self.assertIn("commit", terms)
        self.assertFalse(any(term.startswith("#") for term in terms))

    def test_prompt_limit_drops_from_the_end(self):
        self.assertEqual(hints_within_limit(["aaaa", "bbbb", "cccc"], max_chars=10), (2, 1))
        self.assertEqual(hints_within_limit(["a" * 11], max_chars=10), (0, 1))
        self.assertEqual(hints_within_limit([]), (0, 0))
        many = hint_list(personal(), [], [f"term{i:03}" for i in range(200)])
        kept, dropped = hints_within_limit(many)
        self.assertEqual(many[:5], ["Nimbus-Deck", "Velatrix", "tarvo-kit", "kubectl", "Grafana"])
        self.assertGreater(kept, 5)
        self.assertEqual(kept + dropped, len(many))
        self.assertLessEqual(len(", ".join(many[:kept])), vocab.PROMPT_MAX_CHARS)


class MatcherTest(unittest.TestCase):
    def setUp(self):
        self.matcher = Matcher(personal(), ["zeta-board"], GENERIC)

    def check(self, text, expected):
        self.assertEqual(self.matcher.apply(text), expected)

    def test_close_misrecognitions_get_the_canonical_spelling(self):
        self.check("abre o nimbus deck agora", "abre o Nimbus-Deck agora")
        self.check("o velatriz está pronto", "o Velatrix está pronto")
        self.check("o belatrix", "o belatrix")  # the first letter must match
        self.check("corre o tarvo kit.", "corre o tarvo-kit.")
        self.check("Tarvo kit primeiro", "Tarvo-kit primeiro")  # lowercase entry takes the capital
        self.check("abre o zeta bord", "abre o zeta-board")  # a runtime name, one edit
        self.check("o graphana caiu", "o Grafana caiu")  # ph/f fold
        self.check("o GRAFANA caiu", "o Grafana caiu")  # a capitalized entry keeps its spelling
        self.check("o velatrix", "o Velatrix")
        self.check("vê o dashbord", "vê o dashboard")

    def test_declared_variants(self):
        self.check("usa o cube control aqui", "usa o kubectl aqui")
        self.check("abre o Nimbos Deque", "abre o Nimbus-Deck")

    def test_right_words_are_untouched(self):
        for text in ("Commit feito.", "COMMIT", "o Nimbus-Deck", "Velatrix, Grafana e kubectl", "um pull request",
                     "Pull Request", "Dash board", "comit"):
            self.check(text, text)
        self.assertEqual(self.matcher.replacements("o Commit do Nimbus-Deck"), [])

    def test_near_miss_words_do_not_change(self):
        for text in (
            "a vela e a atriz",           # two words that do not join into a name
            "nimbus",                     # one part of a two-word name
            "o tarvo",                    # too short for fuzzy matching
            "comet o código",             # "commit" is short: exact or variant only
            "os workflows e os dashboards",  # plurals are inflections
            "dois pull requests",
            "o velatrizes",               # two edits away
            "a zeta, board",              # never across punctuation
            "cube controlo",              # variants match exactly only
            "7 dias",
        ):
            self.check(text, text)

    def test_multi_word_spans_do_not_swallow_neighbours(self):
        self.check("o nimbus deck e o velatriz", "o Nimbus-Deck e o Velatrix")
        self.check("do zeta bord e", "do zeta-board e")

    def test_ambiguous_spans_are_left_alone(self):
        matcher = Matcher(parse_vocabulary({"names": ["Corvalen", "Corvalon"]}))
        self.assertEqual(matcher.apply("o corvalan"), "o corvalan")
        self.assertEqual(matcher.apply("o corvalem"), "o Corvalen")

    def test_first_entry_owns_a_shared_key(self):
        matcher = Matcher(parse_vocabulary({"names": ["Tarvo-Kit"]}), ["tarvo kit"], ["tarvokit"])
        self.assertEqual(matcher.apply("o tarvo kit"), "o Tarvo-Kit")

    def test_replacements_report_offsets_and_kind(self):
        text = "o velatriz usa cube control"
        changes = self.matcher.replacements(text)
        self.assertEqual([(text[c.start:c.end], c.text, c.kind) for c in changes],
                         [("velatriz", "Velatrix", "name"), ("cube control", "kubectl", "term")])
        self.assertNotIn("Velatrix", repr(changes[0]))

    def test_empty_vocabulary_changes_nothing(self):
        self.assertEqual(Matcher().apply("o nimbus deck e o velatriz"), "o nimbus deck e o velatriz")


if __name__ == "__main__":
    unittest.main()
