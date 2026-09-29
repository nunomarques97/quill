"""quill.vocabulary tests with invented names and terms only."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        # Generic terms before personal ones; a repeated word keeps its place and the personal spelling.
        self.assertEqual(words, ["Nimbus-Deck", "Velatrix", "tarvo-kit", "zeta-board", "Grafana", "commit", "kubectl"])
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
        self.assertEqual(many[:5], ["Nimbus-Deck", "Velatrix", "tarvo-kit", "term000", "term001"])
        self.assertEqual(many[-2:], ["kubectl", "Grafana"])  # personal terms are dropped first
        self.assertGreater(kept, 5)
        self.assertEqual(kept + dropped, len(many))
        self.assertLessEqual(len(", ".join(many[:kept])), vocab.HINT_MAX_CHARS)

    def test_whisper_gets_the_hints_within_the_cap(self):
        many = hint_list(personal(), [], [f"term{i:03}" for i in range(200)])
        kept, _ = hints_within_limit(many)
        self.assertEqual(vocab.whisper_hints(personal(), [], [f"term{i:03}" for i in range(200)]), many[:kept])
        self.assertLessEqual(len(", ".join(many[:kept])), vocab.HINT_MAX_CHARS)
        self.assertGreater(len(", ".join(many[: kept + 1])), vocab.HINT_MAX_CHARS)
        self.assertEqual(vocab.whisper_hints(personal(), [], ["commit"], max_chars=11), ["Nimbus-Deck"])
        # The generic terms alone leave room for the personal names (the measured Phase 2 list fits).
        generic = vocab.load_generic_terms()
        self.assertEqual(len(vocab.whisper_hints(vocab.EMPTY, [], generic)), len(generic))


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


class FileCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "vocabulary.toml"

    def write(self, text):
        self.path.write_bytes(text.encode("utf-8"))

    def read(self):
        return self.path.read_bytes().decode("utf-8")

    def loaded(self):
        return vocab.load_vocabulary(self.path)


COMMENTED = """# Invented vocabulary. A comment that must stay.
names = ["Zulo", "Anta-Bar"]  # names comment

# Terms, one per line.
terms = [
    "kubectl",  # a trailing comment
    "Grafana",
]

[variants]
# Variant comment.
"kubectl" = ["cube control"]
Zulo = ['zoo lo']
"""


class AddTest(FileCase):
    def test_terms_names_and_variants_keep_every_comment_and_entry(self):
        self.write(COMMENTED)
        result = vocab.add_entries(self.path, terms=["Bollinger", "stop-loss"], names=["Corvalen"],
                                   variants=[("kubectl", "cube cuttle"), ("Anta-Bar", "anta barra"),
                                             ("Bollinger", "bolinjer")])
        self.assertEqual((result.added, result.skipped, result.backup), (6, 0, None))
        text = self.read()
        for comment in ("# Invented vocabulary.", "# names comment", "# Terms, one per line.", "# a trailing comment",
                        "# Variant comment."):
            self.assertIn(comment, text)
        loaded = self.loaded()
        self.assertEqual([e.text for e in loaded.names], ["Zulo", "Anta-Bar", "Corvalen"])
        self.assertEqual([e.text for e in loaded.terms], ["kubectl", "Grafana", "Bollinger", "stop-loss"])
        by_text = {e.text: e.variants for e in loaded.entries}
        self.assertEqual(by_text["kubectl"], ("cube control", "cube cuttle"))
        self.assertEqual(by_text["Zulo"], ("zoo lo",))
        self.assertEqual(by_text["Anta-Bar"], ("anta barra",))
        self.assertEqual(by_text["Bollinger"], ("bolinjer",))
        self.assertEqual(result.vocabulary, loaded)
        self.assertIn('    "stop-loss",\n]', text)  # the multi-line layout is kept
        self.assertIn('names = ["Zulo", "Anta-Bar", "Corvalen"]  # names comment', text)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])  # no backup, no temporary file

    def test_duplicates_are_skipped_ignoring_case_and_accents(self):
        self.write(COMMENTED)
        before = self.read()
        result = vocab.add_entries(self.path, terms=["GRAFANA", "kubéctl", "zulo"], names=["anta bar"],
                                   variants=[("kubectl", "Cube Control"), ("zulo", "zoo-lo")])
        self.assertEqual((result.added, result.skipped), (0, 6))
        self.assertEqual(self.read(), before)  # nothing written

    def test_repeats_within_one_call_are_added_once(self):
        vocab.add_entries(self.path, terms=["Bollinger", "bollinger"])
        self.assertEqual([e.text for e in self.loaded().terms], ["Bollinger"])

    def test_missing_file_is_created(self):
        result = vocab.add_entries(self.path, terms=["kubectl"], names=["Zulo"], variants=[("Zulo", "zoo lo")])
        self.assertEqual(result.added, 3)
        loaded = self.loaded()
        self.assertEqual([(e.text, e.kind, e.variants) for e in loaded.entries],
                         [("Zulo", "name", ("zoo lo",)), ("kubectl", "term", ())])
        self.assertTrue(self.read().startswith("# Personal vocabulary"))

    def test_empty_and_missing_fields_and_tables(self):
        cases = {
            'names = []\nterms = []\n': 'terms = [\n    "Bollinger",\n]',
            '# only a comment\n': 'terms = ["Bollinger"]',
            'names = ["Zulo"]\n[variants]\n': 'terms = ["Bollinger"]\n\n[variants]',
            'terms = ["kubectl",\n  "Grafana"]\n': '  "Grafana",\n  "Bollinger"]',
            'terms = ["kubectl",]\n': 'terms = ["kubectl", "Bollinger",]',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.write(text)
                result = vocab.add_entries(self.path, terms=["Bollinger"], clock=lambda: 0)
                self.assertIsNone(result.backup)
                self.assertIn(expected, self.read())
                self.assertEqual(self.loaded().terms[-1].text, "Bollinger")

    def test_variant_goes_to_a_new_line_or_a_new_table(self):
        self.write('names = ["Zulo"]\n[variants]\n# keep\n\n')
        vocab.add_entries(self.path, variants=[("Zulo", "zoo lo")])
        self.assertEqual(self.read(), 'names = ["Zulo"]\n[variants]\n# keep\n"Zulo" = ["zoo lo"]\n\n')
        self.write('names = ["Zulo"]\n')
        vocab.add_entries(self.path, variants=[("zulo", "zoo lo")])  # the entry is found ignoring case
        self.assertEqual(self.loaded().names[0].variants, ("zoo lo",))
        self.assertIn('[variants]\n"Zulo" = ["zoo lo"]', self.read())

    def test_unusual_layout_is_rewritten_with_a_backup(self):
        original = 'names = ["Zulo"]\nvariants = { Zulo = ["zoo lo"] }  # inline table\n'
        self.write(original)
        result = vocab.add_entries(self.path, terms=["kubectl"], variants=[("Zulo", "zu lu")], clock=lambda: 0)
        self.assertIsNotNone(result.backup)
        self.assertEqual(result.backup.read_text(encoding="utf-8"), original)
        loaded = self.loaded()
        self.assertEqual(loaded.names[0].variants, ("zoo lo", "zu lu"))
        self.assertEqual([e.text for e in loaded.terms], ["kubectl"])
        second = vocab.add_entries(self.path, terms=["Grafana"], clock=lambda: 0)
        self.assertIsNone(second.backup)  # the rewritten layout is edited in place

    def test_errors_name_the_option_never_the_value_and_change_nothing(self):
        self.write(COMMENTED)
        before = self.read()
        cases = [
            (dict(terms=["--"]), "--add value 1 must have letters"),
            (dict(names=["x" * 61]), "--add-name value 1 must be at most 60"),
            (dict(terms=["Zeta Secreta um dois tres"]), "at most 4 words"),
            (dict(variants=[("Secretname", "x")]), "--add-variant pair 1 entry is not a listed"),
            (dict(variants=[("kubectl", "Grafana")]), "--add-variant pair 1 spoken form repeats terms[1]"),
            (dict(variants=[("Grafana", "zoo lo")]), "spoken form repeats names[0]"),
            (dict(terms=["ok", "tab\there"]), "--add value 2 must be at most 60 printable"),
        ]
        for kwargs, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(VocabularyError) as caught:
                    vocab.add_entries(self.path, **kwargs)
                self.assertIn(message, str(caught.exception))
                for value in ("Secretname", "Zeta", "Grafana", "zoo lo"):
                    self.assertNotIn(value, str(caught.exception))
                self.assertEqual(self.read(), before)

    def test_limits_and_invalid_files_are_refused(self):
        # Letters between the digits: doubled characters fold into one.
        self.write("terms = [" + ", ".join(f'"a{i // 100}b{i // 10 % 10}c{i % 10}"' for i in range(vocab.MAX_ENTRIES))
                   + "]\n")
        with self.assertRaises(VocabularyError) as caught:
            vocab.add_entries(self.path, names=["Zulo"])
        self.assertIn(f"more than {vocab.MAX_ENTRIES}", str(caught.exception))
        self.write('names = ["Zulo"\n')
        with self.assertRaises(VocabularyError):
            vocab.add_entries(self.path, terms=["kubectl"])
        self.assertEqual(self.read(), 'names = ["Zulo"\n')

    def test_windows_line_endings_are_kept(self):
        self.write(COMMENTED.replace("\n", "\r\n"))
        result = vocab.add_entries(self.path, terms=["Bollinger"], variants=[("Anta-Bar", "anta barra")])
        self.assertIsNone(result.backup)
        text = self.read()
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn('    "Bollinger",\r\n]', text)
        self.assertIn("# a trailing comment", text)
        self.assertEqual(self.loaded().names[1].variants, ("anta barra",))

    def test_a_sharing_violation_on_the_rename_is_retried(self):
        self.write(COMMENTED)
        real, failures = vocab.os.replace, []

        def flaky(source, target):
            if not failures:
                failures.append(1)
                raise PermissionError(32, "sharing violation")
            return real(source, target)

        with mock.patch.object(vocab.os, "replace", flaky):
            vocab.add_entries(self.path, terms=["Bollinger"])
        self.assertEqual(failures, [1])
        self.assertEqual(self.loaded().terms[-1].text, "Bollinger")

    def test_a_failed_write_leaves_the_file_and_no_temporary(self):
        self.write(COMMENTED)
        with mock.patch.object(vocab.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                vocab.add_entries(self.path, terms=["Bollinger"])
        self.assertEqual(self.read(), COMMENTED)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class AddCliTest(FileCase):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = vocab.main([*args, "--file", str(self.path)])
        return code, out.getvalue(), err.getvalue()

    def test_add_prints_counts_only(self):
        self.write(COMMENTED)
        code, out, err = self.run_main("--add", "Bollinger", "--add", "grafana", "--add-name", "Corvalen",
                                       "--add-variant", "Bollinger", "bolinjer")
        self.assertEqual(code, 0, err)
        self.assertIn("3 added, 1 already listed", out)
        self.assertIn("3 names, 3 terms, 3 variants", out)
        self.assertRegex(out, r"hints \d+ of \d+ fit the prompt \(330 characters\), \d+ dropped")
        for value in ("Bollinger", "Corvalen", "bolinjer", "Grafana", str(self.path.parent)):
            self.assertNotIn(value, out)

    def test_errors_exit_1_and_name_the_field(self):
        self.write(COMMENTED)
        code, out, err = self.run_main("--add-variant", "Secretname", "x")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("entry is not a listed name or term; nothing changed", err)
        self.assertNotIn("Secretname", err)
        with mock.patch.object(vocab, "_write_atomic", side_effect=PermissionError(13, "denied")):
            code, out, err = self.run_main("--add", "Bollinger")
        self.assertEqual(code, 1)
        self.assertIn("not written (PermissionError); nothing changed", err)
        self.assertEqual(self.read(), COMMENTED)

    def test_add_and_check_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(vocab.main(["--check", str(self.path), "--add", "x"]), 2)


class VocabularyFileTest(FileCase):
    def setUp(self):
        super().setUp()
        self.write('names = ["Zulo"]\n')
        self.source = vocab.VocabularyFile(self.path, sleep=lambda s: None)
        self.first = self.source.load()

    def test_unchanged_file_is_not_read_again(self):
        with mock.patch.object(vocab, "parse_text", wraps=vocab.parse_text) as parse:
            self.assertIsNone(self.source.refresh())
            self.assertIsNone(self.source.refresh())
        parse.assert_not_called()
        self.assertIs(self.source.vocabulary, self.first)

    def test_changed_file_gives_the_new_vocabulary_once(self):
        vocab.add_entries(self.path, terms=["kubectl"])
        changed = self.source.refresh()
        self.assertEqual([e.text for e in changed.entries], ["Zulo", "kubectl"])
        self.assertIs(self.source.vocabulary, changed)
        self.assertIsNone(self.source.refresh())

    def test_same_content_saved_again_is_no_change(self):
        vocab._write_atomic(self.path, self.path.read_bytes())
        self.assertIsNone(self.source.refresh())
        self.assertIs(self.source.vocabulary, self.first)

    def test_invalid_file_keeps_the_previous_vocabulary_and_logs_once(self):
        self.write('names = ["Secretname", "secretname"]\n')
        with self.assertLogs("quill.vocabulary", level="WARNING") as logs:
            self.assertIsNone(self.source.refresh())
        self.assertIn("names[1] repeats names[0]; the previous vocabulary stays", logs.output[0])
        self.assertNotIn("Secretname", "\n".join(logs.output))
        self.assertIs(self.source.vocabulary, self.first)
        with mock.patch.object(vocab, "parse_text") as parse:
            self.assertIsNone(self.source.refresh())  # the same invalid file: not read or logged again
        parse.assert_not_called()
        self.write('names = ["Zulo", "Anta"]\n')
        self.assertEqual(len(self.source.refresh().names), 2)

    def test_unreadable_file_is_tried_again(self):
        vocab.add_entries(self.path, terms=["kubectl"])
        ticks = iter(range(1000))
        self.source.monotonic = lambda: next(ticks) / 10  # 0.1 s per call: the retries end at once
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(32, "sharing violation")):
            with self.assertLogs("quill.vocabulary", level="WARNING") as logs:
                self.assertIsNone(self.source.refresh())
        self.assertIn("not readable (PermissionError)", logs.output[0])
        self.assertIs(self.source.vocabulary, self.first)
        self.assertEqual(len(self.source.refresh().terms), 1)

    def test_removed_file_is_an_empty_vocabulary(self):
        self.path.unlink()
        self.assertIs(self.source.refresh(), vocab.EMPTY)
        self.assertIs(self.source.vocabulary, vocab.EMPTY)

    def test_load_of_an_invalid_file_raises(self):
        self.write("names = 1\n")
        with self.assertRaises(VocabularyError):
            vocab.VocabularyFile(self.path).load()


if __name__ == "__main__":
    unittest.main()
