"""Personal vocabulary: Whisper hints and post-recognition term/name matching.

The personal vocabulary lives in the ignored ``local/vocabulary.toml`` (the
committed ``vocabulary.example.toml`` shows the format with invented entries):

    names = ["Nimbus-Deck"]              # project names, spelled as typed
    terms = ["kubectl"]                  # technical terms
    [variants]
    "Nimbus-Deck" = ["nimbos deque"]     # spoken or misheard forms

A missing file is an empty vocabulary. An invalid one raises
``VocabularyError``; messages name the field only, never its value, because
the values are personal.

Hints (``hint_list``), in priority order, duplicates removed ignoring case:

1. personal project names, in file order;
2. runtime project names (for example the benchmark's resolved names);
3. personal terms, in file order;
4. generic terms (``bench/terms_en.txt``).

Whisper's prompt holds at most ``PROMPT_MAX_CHARS`` characters of
vocabulary, joined in this order; the first entry that does not fit ends the
list (``quill.whisper.join_vocabulary``), so names are the last to be dropped.
Variants are never hints: they are what Whisper writes by mistake.

The matcher (``Matcher.apply``) runs after recognition and cleanup. It maps a
span of one or more words to an entry's spelling when, after folding (accents
and case removed, letters and digits only, ``y``->``i``, ``k``->``c``,
``ph``->``f``, doubled letters collapsed), the span equals the entry or one
of its variants, or is within a small edit distance of the entry:

- entries shorter than ``MIN_FUZZY_CHARS`` folded characters (``run``,
  ``log``) change only on an exact folded match or a declared variant;
- the edit distance bound is 1 from ``MIN_FUZZY_CHARS`` characters and 2
  from ``TWO_EDITS_CHARS``; the first letter must be the same, and a span
  that is the entry plus a suffix (a plural such as ``workflows``) is an
  inflection, not a misrecognition;
- a span has at most as many words as the entry, joined only by spaces or
  hyphens (never across other punctuation) and never contains a number;
- two different entries equally close to one span are ambiguous: the span
  is left alone.

An entry with a capital or a digit (``Nimbus-Deck``) is always written in
its own spelling. An all-lowercase entry takes the span's first capital
(``tarvo-kit`` at a sentence start becomes ``Tarvo-kit``), and an
all-lowercase term is left as is whenever the span already folds to it,
whatever its case or spacing (``GitHub`` and ``VS Code`` stay). Only the vocabulary is used, never
reference text, and words that match no entry are never changed.

Usage: py -3.12 -m quill.vocabulary --check [PATH]
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from quill.whisper import PROMPT_MAX_CHARS

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_VOCABULARY = REPO_ROOT / "local" / "vocabulary.toml"
EXAMPLE_VOCABULARY = REPO_ROOT / "vocabulary.example.toml"
# Generic English and technical terms (committed; shared with the benchmark).
GENERIC_TERMS = REPO_ROOT / "bench" / "terms_en.txt"

MAX_ENTRIES = 500
MAX_VARIANTS = 20
MAX_ENTRY_CHARS = 60
MAX_ENTRY_WORDS = 4
MIN_FUZZY_CHARS = 7
TWO_EDITS_CHARS = 12

FIELDS = ("names", "terms", "variants")
# A word: letters or digits, with inner hyphens, apostrophes or dots.
WORD = re.compile(r"[^\W_]+(?:[-'’.][^\W_]+)*")
# What may separate the words of one span.
JOINER = re.compile(r"^[ \t-]+$")


class VocabularyError(ValueError):
    """The vocabulary file is invalid; the message names the field, never its value."""


# ---------------------------------------------------------------- folding


def fold(text: str) -> str:
    """Matching key: no accents or case, letters and digits only, a few spellings merged."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    key = "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))
    key = key.replace("ph", "f").replace("y", "i").replace("k", "c")
    return re.sub(r"(.)\1+", r"\1", key)


def max_edits(length: int) -> int:
    if length >= TWO_EDITS_CHARS:
        return 2
    if length >= MIN_FUZZY_CHARS:
        return 1
    return 0


def distance(a: str, b: str, bound: int) -> int:
    """Optimal string alignment distance, or ``bound + 1`` once it exceeds ``bound``."""
    if abs(len(a) - len(b)) > bound:
        return bound + 1
    before: list[int] | None = None
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if before is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], before[j - 2] + 1)
        if min(current) > bound:
            return bound + 1
        before, previous = previous, current
    return min(previous[-1], bound + 1)


# ---------------------------------------------------------------- vocabulary


@dataclass(frozen=True)
class Entry:
    """One canonical spelling and its declared spoken variants."""

    text: str = field(repr=False)
    kind: str  # "name" or "term"
    variants: tuple[str, ...] = field(default=(), repr=False)

    @property
    def explicit_case(self) -> bool:
        """Written exactly as listed: it has a capital or a digit."""
        return self.text != self.text.lower() or any(ch.isdigit() for ch in self.text)

    def written(self, span: str) -> bool:
        """``span`` already stands for this entry: a lowercase term in any case or spacing
        ("GitHub", "VS Code"), otherwise the entry's spelling itself."""
        if self.kind == "term" and not self.explicit_case and fold(span) == fold(self.text):
            return True
        return self.spelling(span) == span

    def spelling(self, span: str) -> str:
        return self.text if self.explicit_case else _adapt_case(self.text, span)


@dataclass(frozen=True)
class Vocabulary:
    """Personal names and terms, in file order."""

    names: tuple[Entry, ...] = ()
    terms: tuple[Entry, ...] = ()

    @property
    def entries(self) -> tuple[Entry, ...]:
        return self.names + self.terms

    def counts(self) -> dict[str, int]:
        return {
            "names": len(self.names),
            "terms": len(self.terms),
            "variants": sum(len(entry.variants) for entry in self.entries),
        }


EMPTY = Vocabulary()


def _entry_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise VocabularyError(f"vocabulary: {field_name} must be a string")
    text = " ".join(value.split())
    if not text or not fold(text):
        raise VocabularyError(f"vocabulary: {field_name} must have letters or digits")
    if len(text) > MAX_ENTRY_CHARS or any(unicodedata.category(ch)[0] == "C" for ch in value):
        raise VocabularyError(f"vocabulary: {field_name} must be at most {MAX_ENTRY_CHARS} printable characters")
    if len(WORD.findall(text)) > MAX_ENTRY_WORDS:
        raise VocabularyError(f"vocabulary: {field_name} must have at most {MAX_ENTRY_WORDS} words")
    return text


def _text_list(value: object, field_name: str, limit: int) -> list[str]:
    if not isinstance(value, list):
        raise VocabularyError(f"vocabulary: {field_name} must be a list of strings")
    if len(value) > limit:
        raise VocabularyError(f"vocabulary: {field_name} has more than {limit} entries")
    return [_entry_text(item, f"{field_name}[{index}]") for index, item in enumerate(value)]


def parse_vocabulary(data: dict[str, object]) -> Vocabulary:
    """A validated vocabulary from the TOML tables of a vocabulary file."""
    for key in data:
        if key not in FIELDS:
            raise VocabularyError(f"vocabulary: unknown field {key}")
    names = _text_list(data.get("names", []), "names", MAX_ENTRIES)
    terms = _text_list(data.get("terms", []), "terms", MAX_ENTRIES)
    if len(names) + len(terms) > MAX_ENTRIES:
        raise VocabularyError(f"vocabulary: names and terms have more than {MAX_ENTRIES} entries")
    variants_table = data.get("variants", {})
    if not isinstance(variants_table, dict):
        raise VocabularyError("vocabulary: variants must be a table")
    owners: dict[str, str] = {}
    for index, text in enumerate([*names, *terms]):
        field_name = f"names[{index}]" if index < len(names) else f"terms[{index - len(names)}]"
        if fold(text) in owners:
            raise VocabularyError(f"vocabulary: {field_name} repeats {owners[fold(text)]}")
        owners[fold(text)] = field_name
    variants: dict[str, tuple[str, ...]] = {}
    for position, (key, value) in enumerate(variants_table.items()):
        field_name = f"variants entry {position + 1}"
        if not any(key == text for text in [*names, *terms]):
            raise VocabularyError(f"vocabulary: {field_name} is not a listed name or term")
        spoken = _text_list(value, field_name, MAX_VARIANTS)
        for item in spoken:
            if fold(item) in owners:
                raise VocabularyError(f"vocabulary: {field_name} repeats {owners[fold(item)]}")
            owners[fold(item)] = field_name
        variants[key] = tuple(spoken)
    return Vocabulary(
        names=tuple(Entry(text, "name", variants.get(text, ())) for text in names),
        terms=tuple(Entry(text, "term", variants.get(text, ())) for text in terms),
    )


def _label(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def load_vocabulary(path: Path = LOCAL_VOCABULARY) -> Vocabulary:
    """The vocabulary in ``path``; a missing file is an empty vocabulary."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return EMPTY
    except tomllib.TOMLDecodeError as exc:
        # The decoder reports a position only, never the offending text.
        raise VocabularyError(f"vocabulary: {_label(path)} is not valid TOML: {exc}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise VocabularyError(f"vocabulary: {_label(path)} cannot be read ({type(exc).__name__})") from None
    return parse_vocabulary(data)


# ---------------------------------------------------------------- hints


def _dedupe(words: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        word = " ".join(word.split())
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            out.append(word)
    return out


def hint_list(vocabulary: Vocabulary, extra_names: Iterable[str] = (), generic_terms: Iterable[str] = ()) -> list[str]:
    """Hint words in priority order: personal names, extra names, personal terms, generic terms."""
    return _dedupe([
        *(entry.text for entry in vocabulary.names),
        *extra_names,
        *(entry.text for entry in vocabulary.terms),
        *generic_terms,
    ])


def hints_within_limit(hints: Sequence[str], max_chars: int = PROMPT_MAX_CHARS) -> tuple[int, int]:
    """(kept, dropped): how many hint words fit Whisper's prompt, as ``join_vocabulary`` joins them."""
    out, kept = "", 0
    for word in hints:
        candidate = word if not out else out + ", " + word
        if len(candidate) > max_chars:
            break
        out, kept = candidate, kept + 1
    return kept, len(hints) - kept


def load_generic_terms(path: Path = GENERIC_TERMS) -> list[str]:
    """The committed generic terms, one per line; ``#`` starts a comment."""
    terms = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            terms.append(line)
    return terms


# ---------------------------------------------------------------- matcher


@dataclass(frozen=True)
class _Target:
    key: str
    entry: Entry = field(repr=False)
    words: int
    fuzzy: bool  # False for declared variants: exact folded match only


@dataclass(frozen=True)
class Replacement:
    """One span changed by the matcher (character offsets in the input text)."""

    start: int
    end: int
    text: str = field(repr=False)
    kind: str


def _words(text: str) -> int:
    """Parts of an entry: runs of letters or digits ("Nimbus-Deck" has two)."""
    return len(re.findall(r"[^\W_]+", text))


def _adapt_case(spelling: str, span: str) -> str:
    if span[:1].isupper() and spelling[:1].islower():
        return spelling[:1].upper() + spelling[1:]
    return spelling


class Matcher:
    """Maps close misrecognitions to vocabulary spellings; everything else is untouched."""

    def __init__(self, vocabulary: Vocabulary = EMPTY, extra_names: Iterable[str] = (), generic_terms: Iterable[str] = ()):
        entries: list[Entry] = list(vocabulary.names)
        entries += [Entry(" ".join(name.split()), "name") for name in extra_names if fold(name)]
        entries += list(vocabulary.terms)
        entries += [Entry(" ".join(term.split()), "term") for term in generic_terms if fold(term)]
        targets: list[_Target] = []
        seen: set[str] = set()
        for entry in entries:  # priority order: the first entry owns a key
            for text, fuzzy in ((entry.text, True), *((variant, False) for variant in entry.variants)):
                key = fold(text)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(_Target(key, entry, _words(text), fuzzy))
        self._targets = tuple(targets)
        self._exact = {target.key: target for target in targets}
        self.max_words = max((target.words for target in targets), default=0)

    def _candidates(self, key: str, words: int) -> list[_Target]:
        exact = self._exact.get(key)
        if exact is not None:
            return [exact]
        best: list[tuple[int, _Target]] = []
        for target in self._targets:
            bound = max_edits(len(target.key))
            if (not target.fuzzy or bound == 0 or target.words < words or key[:1] != target.key[:1]
                    or key.startswith(target.key)):
                continue
            found = distance(key, target.key, bound)
            if found <= bound:
                best.append((found, target))
        if not best:
            return []
        low = min(found for found, _ in best)
        return [target for found, target in best if found == low]

    def replacements(self, text: str) -> list[Replacement]:
        """The spans ``apply`` changes, left to right, without overlaps."""
        tokens = list(WORD.finditer(text))
        out: list[Replacement] = []
        index = 0
        while index < len(tokens):
            chosen: tuple[int, Replacement | None] | None = None
            for size in range(min(self.max_words, len(tokens) - index), 0, -1):
                span = tokens[index : index + size]
                gaps = [text[a.end() : b.start()] for a, b in zip(span, span[1:])]
                if any(not JOINER.match(gap) for gap in gaps) or any(t.group().isdigit() for t in span):
                    continue
                source = text[span[0].start() : span[-1].end()]
                candidates = self._candidates(fold(source), size)
                entries = {target.entry for target in candidates}
                if len(entries) != 1:
                    continue  # nothing close, or ambiguous between two entries
                entry = candidates[0].entry
                if entry.written(source):
                    chosen = (size, None)  # already this word: leave it
                else:
                    chosen = (size, Replacement(span[0].start(), span[-1].end(), entry.spelling(source), entry.kind))
                break
            if chosen is None:
                index += 1
                continue
            size, change = chosen
            if change is not None:
                out.append(change)
            index += size
        return out

    def apply(self, text: str) -> str:
        """``text`` with every close misrecognition replaced by the vocabulary spelling."""
        pieces: list[str] = []
        last = 0
        for change in self.replacements(text):
            pieces.append(text[last : change.start])
            pieces.append(change.text)
            last = change.end
        pieces.append(text[last:])
        return "".join(pieces)


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.vocabulary", description=__doc__.splitlines()[0])
    parser.add_argument("--check", nargs="?", const=LOCAL_VOCABULARY, type=Path, metavar="PATH",
                        help="validate PATH (default: local/vocabulary.toml); prints counts only")
    args = parser.parse_args(argv)
    if args.check is None:
        parser.print_usage(sys.stderr)
        return 2
    if not args.check.is_file():
        print(f"vocabulary: {_label(args.check)} not found", file=sys.stderr)
        return 2
    try:
        vocabulary = load_vocabulary(args.check)
    except VocabularyError as exc:
        print(exc, file=sys.stderr)
        return 1
    counts = vocabulary.counts()
    hints = hint_list(vocabulary, generic_terms=load_generic_terms())
    kept, dropped = hints_within_limit(hints)
    print(f"vocabulary: OK ({_label(args.check)}; {counts['names']} names, {counts['terms']} terms, "
          f"{counts['variants']} variants; hints {kept} of {len(hints)} fit the prompt"
          + (f", {dropped} dropped" if dropped else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
