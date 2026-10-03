"""quill.heard tests: pack terms chosen as decoding hints by what has been heard.

Every term, name and sentence is invented. No model, microphone or sound is used.
"""

import unittest

from quill import heard as H
from quill import vocabulary as V
from quill.vocabulary import distance
from quill.whisper import HINTS_PREFIX, PROJECT_HINT_MAX_CHARS, project_terms, session_hints

PACK = ("Nimbus-Deck", "ledger", "Quorvane", "API", "Kelvar", "Dornix", "build.gradle", "GlacierPlanCsvTest", "Zeppa")


class Counting:
    """``quill.vocabulary.distance`` that records the spans it compares."""

    def __init__(self):
        self.spans = []

    def __call__(self, a, b, bound):
        self.spans.append(a)
        return distance(a, b, bound)


class MatcherTest(unittest.TestCase):
    def matches(self, heard, terms=PACK):
        return H.HeardMatcher(terms).matches(heard)

    def test_a_term_that_sounds_like_a_heard_word_matches(self):
        self.assertEqual(self.matches("e depois o quorvan trata disso"), ["Quorvane"])
        self.assertEqual(self.matches("o ledger"), ["ledger"])  # heard right: an exact match

    def test_nothing_that_sounds_close_matches_nothing(self):
        self.assertEqual(self.matches("isto não tem nada a ver com o resto"), [])
        self.assertEqual(self.matches(""), [])
        self.assertEqual(self.matches("   ,. "), [])

    def test_a_term_under_four_folded_letters_needs_an_exact_sound_key(self):
        self.assertEqual(self.matches("a apa"), [])  # one edit from "API": not enough
        self.assertEqual(self.matches("o apy"), ["API"])  # y and i sound alike: the same key
        self.assertEqual(self.matches("a pi"), ["API"])  # two words, joined

    def test_a_multi_part_term_matches_the_joined_key_of_a_multi_word_span(self):
        self.assertEqual(self.matches("abre o nimbos deck"), ["Nimbus-Deck"])
        self.assertEqual(self.matches("abre o nimbos-deck"), ["Nimbus-Deck"])
        self.assertEqual(self.matches("abre o nimbos deck", ("NimbusDeck",)), ["NimbusDeck"])
        self.assertEqual(self.matches("abre o nimbos. Deck"), [])  # never across other punctuation
        self.assertNotIn("nimbosdek", H.span_keys("nim bos de ck"))  # four words: longer than a span

    def test_the_first_letter_must_agree_unless_the_keys_are_equal(self):
        self.assertEqual(self.matches("o pelvar"), [])  # one edit from "Kelvar", another first letter
        self.assertEqual(self.matches("o kelvan"), ["Kelvar"])

    def test_the_edit_bound_grows_with_the_term(self):
        self.assertEqual(H.max_distance("api"), 0)
        self.assertEqual(H.max_distance("zepa"), 1)
        self.assertEqual(H.max_distance("kuoruane"), 2)
        self.assertEqual(self.matches("zepi"), ["Zeppa"])
        self.assertEqual(self.matches("zipi"), [])  # two edits from a four-letter key
        self.assertEqual(self.matches("korvene"), ["Quorvane"])  # two edits from an eight-letter key

    def test_terms_that_are_not_said_aloud_never_match(self):
        self.assertEqual(self.matches("o build gradle e o glacier plan csv test"), [])

    def test_closest_first_ties_in_pack_order(self):
        heard = "o dornis, o kelvan e a zeppa"
        self.assertEqual(self.matches(heard), ["Zeppa", "Kelvar", "Dornix"])  # exact first, then pack order
        self.assertEqual(self.matches(heard, ("Dornix", "Kelvar", "Zeppa")), ["Zeppa", "Dornix", "Kelvar"])
        # The closest span of a term decides: heard exactly later, it moves ahead.
        self.assertEqual(self.matches(heard + " e o kelvar"), ["Kelvar", "Zeppa", "Dornix"])

    def test_repeated_spellings_of_one_key_are_one_term(self):
        matcher = H.HeardMatcher(("Nimbus-Deck", "NimbusDeck", "nimbus deck", "", "  ", 42))
        self.assertEqual([term for _, term, _ in matcher.terms], ["Nimbus-Deck"])
        self.assertEqual(matcher.matches("o nimbos deck"), ["Nimbus-Deck"])

    def test_matching_is_incremental(self):
        counting = Counting()
        matcher = H.HeardMatcher(("Kelvar", "Korvane", "Kaltrix"), distance=counting)
        first = matcher.matches("kelvan korvan")
        self.assertTrue(counting.spans)
        counting.spans.clear()
        self.assertEqual(matcher.matches("kelvan korvan"), first)
        self.assertEqual(counting.spans, [])  # the same words: nothing compared again
        matcher.matches("kelvan korvan kaltrex")
        self.assertTrue(counting.spans)
        self.assertTrue(all(span.endswith("kaltrex") for span in counting.spans))  # only spans with the new word
        counting.spans.clear()
        matcher.matches("kelvan korvan kaltrex")
        self.assertEqual(counting.spans, [])

    def test_the_memo_is_bounded(self):
        matcher = H.HeardMatcher(("Kelvar",))
        matcher.matches(" ".join(f"k{n}" for n in range(H.MAX_MEMO + 10)))
        self.assertLessEqual(len(matcher._memo), H.MAX_MEMO)

    def test_span_keys(self):
        self.assertEqual(H.span_keys("a pi a"), ["a", "api", "apia", "pi", "pia"])
        self.assertEqual(H.span_keys("um, dois três"), ["um", "dois", "doistres", "tres"])


class SelectionTest(unittest.TestCase):
    NAMES = ("Ana Lima", "Nimbus-Deck")
    GENERIC = tuple(f"generic{n:02d}" for n in range(40))

    def setUp(self):
        self.vocabulary = V.Vocabulary(names=tuple(V.Entry(name, "name") for name in self.NAMES))
        self.today = V.whisper_hints(self.vocabulary, (), self.GENERIC)

    def source(self, terms=PACK, project="orchard"):
        return H.HeardHints(project, terms, self.vocabulary, self.GENERIC)

    def test_nothing_heard_gives_exactly_todays_project_hints(self):
        source = self.source()
        todays = V.whisper_hints(self.vocabulary, project_terms("orchard", PACK, self.today), self.GENERIC)
        self.assertEqual(source.words(""), todays)
        self.assertEqual(source.words("nada de parecido aqui"), todays)
        self.assertEqual(source(""), session_hints(todays))
        self.assertEqual(source.initial, session_hints(todays))
        self.assertEqual((source.prompt, source.hotwords, source.language),
                         (source.initial.prompt, source.initial.hotwords, None))
        self.assertEqual(project_terms("orchard", PACK, self.today, first=()), project_terms("orchard", PACK, self.today))

    def test_heard_terms_come_first_in_the_project_part_then_todays_order(self):
        words = self.source().words("o kelvan e a zeppa")
        names = list(self.NAMES)
        self.assertEqual(words[:2], names)
        # Heard terms first (closest first), then the project name and the rest in the pack's order.
        self.assertEqual(words[2:7], ["Zeppa", "Kelvar", "orchard", "ledger", "Quorvane"])
        self.assertEqual(words.count("Kelvar"), 1)
        self.assertNotIn("build.gradle", words)

    def test_heard_terms_already_in_todays_hints_are_skipped(self):
        words = self.source().words("o nimbos deck")  # a personal name: already a hint
        self.assertEqual(words.count("Nimbus-Deck"), 1)
        self.assertEqual(words, self.source().words(""))
        self.assertEqual(project_terms("orchard", ("Zeppa",), known=("zeppa",), first=["Zeppa"]), ["orchard"])
        self.assertEqual(project_terms("orchard", ("Zeppa",), first=["Zeppa", "zeppa", "a.b"]), ["Zeppa", "orchard"])

    def test_the_budgets_hold_with_a_long_heard_text_and_a_large_pack(self):
        pack = tuple(f"Termo{chr(97 + n // 26)}{chr(97 + n % 26)}vex" for n in range(150))
        heard = " ".join(term.lower().replace("vex", "vez") for term in pack) * 3
        source = self.source(pack)
        matched = source.matcher.matches(heard)
        self.assertGreater(len(matched), 100)
        self.assertEqual(len(matched), len(source.matcher.terms))  # every spoken key was heard
        part = project_terms("orchard", pack, self.today, first=matched)
        self.assertLessEqual(len(", ".join(part)), PROJECT_HINT_MAX_CHARS)
        self.assertEqual(part[0], matched[0])
        words = source.words(heard)
        self.assertLessEqual(len(", ".join(words)), V.HINT_MAX_CHARS)
        self.assertEqual(words[2:2 + len(part)], part)
        hints = source(heard)
        self.assertLessEqual(len(hints.prompt), len(HINTS_PREFIX) + V.HINT_MAX_CHARS + 1)

    def test_the_same_heard_text_gives_the_same_hints(self):
        source = self.source()
        first = source("o kelvan e a zeppa")
        self.assertEqual(source("o kelvan e a zeppa"), first)
        self.assertEqual(self.source()("o kelvan e a zeppa"), first)
        self.assertNotEqual(first, source(""))


if __name__ == "__main__":
    unittest.main()
