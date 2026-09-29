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
3. generic terms (``bench/terms_en.txt``);
4. personal terms, in file order.

Whisper gets at most ``HINT_MAX_CHARS`` characters of hints
(``whisper_hints``), joined in this order; the first entry that does not fit
ends the list, so names are the last to be dropped and personal terms the
first. The hints go into both the hotwords and the initial prompt, next to up
to 200 characters of earlier text; Whisper was trained with at most 223
prompt tokens, and a longer list made recognition worse on the real dictation
set. The generic terms come before the personal ones because the measured
Phase 2 list already fills the room: personal terms ahead of them cost
English terms and intent there. A personal term that does not fit is still
matched after recognition.
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

Adding entries (``add_entries``, ``--add``) edits the file in place: the new
spellings are inserted into the ``names`` or ``terms`` array, or a variant
into its ``[variants]`` line, so every other entry, variant and comment is
kept. The edited text is parsed again and must give exactly the old
vocabulary plus the new entries; when it does not (an unusual layout), the
file is rewritten from its data instead and the previous file is kept next
to it as ``vocabulary.toml.bak-<time>``. Entries that are already listed
(ignoring case and accents, as the matcher does) are skipped. Writes are
atomic (temporary file and rename).

The running app reloads a changed file before the next dictation
(``VocabularyFile.refresh``); an invalid file keeps the previous vocabulary.

Usage:
    py -3.12 -m quill.vocabulary --check [PATH]
    py -3.12 -m quill.vocabulary --add TERM [--add TERM ...]
    py -3.12 -m quill.vocabulary --add-name NAME
    py -3.12 -m quill.vocabulary --add-variant ENTRY SPOKEN
    (``--file PATH`` edits another file than local/vocabulary.toml)

Output is counts only, never an entry.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import tomllib
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TypeVar


log = logging.getLogger("quill.vocabulary")
T = TypeVar("T")

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_VOCABULARY = REPO_ROOT / "local" / "vocabulary.toml"
EXAMPLE_VOCABULARY = REPO_ROOT / "vocabulary.example.toml"
# Generic English and technical terms (committed; shared with the benchmark).
GENERIC_TERMS = REPO_ROOT / "bench" / "terms_en.txt"

MAX_ENTRIES = 500
# Measured on the real dictation set: the Phase 2 list (41 hints, 327
# characters, about 210 prompt tokens with the earlier text) fits; 480
# characters raised the clean WER from 10.5 % to 10.9 % and 600 to 36.2 %.
HINT_MAX_CHARS = 330
MAX_VARIANTS = 20
MAX_ENTRY_CHARS = 60
MAX_ENTRY_WORDS = 4
MIN_FUZZY_CHARS = 7
TWO_EDITS_CHARS = 12
RETRY_S = 0.5  # how long a Windows sharing violation on the file is retried
RETRY_FIRST_S = 0.005
RETRY_MAX_S = 0.05

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


def _retry(action: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep,
           monotonic: Callable[[], float] = time.monotonic) -> T:
    """``action()``, retried while Windows reports a sharing violation, for up to ``RETRY_S``.

    A reader opening the file during the rename of a save, or a save renaming
    over a file a reader holds open, gets ``PermissionError`` for a moment.
    """
    deadline = monotonic() + RETRY_S
    delay = RETRY_FIRST_S
    while True:
        try:
            return action()
        except PermissionError:
            if monotonic() + delay > deadline:
                raise
        sleep(delay)
        delay = min(delay * 2, RETRY_MAX_S)


def parse_text(raw: bytes, path: Path) -> Vocabulary:
    """The validated vocabulary in the bytes of the file ``path``."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        # The decoder reports a position only, never the offending text.
        raise VocabularyError(f"vocabulary: {_label(path)} is not valid TOML: {exc}") from None
    except UnicodeDecodeError as exc:
        raise VocabularyError(f"vocabulary: {_label(path)} cannot be read ({type(exc).__name__})") from None
    return parse_vocabulary(data)


def load_vocabulary(path: Path = LOCAL_VOCABULARY) -> Vocabulary:
    """The vocabulary in ``path``; a missing file is an empty vocabulary."""
    path = Path(path)
    try:
        raw = _retry(path.read_bytes)
    except FileNotFoundError:
        return EMPTY
    except OSError as exc:
        raise VocabularyError(f"vocabulary: {_label(path)} cannot be read ({type(exc).__name__})") from None
    return parse_text(raw, path)


class VocabularyFile:
    """The vocabulary file of the running app, read again when it changes on disk.

    ``load`` reads it at start and raises ``VocabularyError`` like
    ``load_vocabulary``. ``refresh`` (before each dictation) compares the
    file's stamp (modification time, size and file id, which an atomic save
    changes) with the one of the last read:
    a changed, valid file gives the new vocabulary; an unchanged file costs
    one ``stat`` and gives ``None``. An invalid file also gives ``None``, so
    the previous vocabulary stays in use; it is logged once per change, with
    the field name only. An unreadable file (still locked after the retries)
    is tried again at the next refresh. A removed file is an empty vocabulary,
    as at start.
    """

    def __init__(self, path: Path = LOCAL_VOCABULARY, *, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(path)
        self.sleep = sleep
        self.monotonic = monotonic
        self.vocabulary = EMPTY
        self._stamp: tuple[int, int, int] | None = None

    def _file_stamp(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _read(self) -> Vocabulary:
        try:
            raw = _retry(self.path.read_bytes, sleep=self.sleep, monotonic=self.monotonic)
        except FileNotFoundError:
            return EMPTY
        return parse_text(raw, self.path)

    def load(self) -> Vocabulary:
        stamp = self._file_stamp()
        try:
            vocabulary = self._read()
        except OSError as exc:
            raise VocabularyError(f"vocabulary: {_label(self.path)} cannot be read ({type(exc).__name__})") from None
        self.vocabulary, self._stamp = vocabulary, stamp
        return vocabulary

    def refresh(self) -> Vocabulary | None:
        """The new vocabulary when the file changed and is valid; otherwise ``None``."""
        # Taken before the read: a save replacing the file in between shows as a change next time.
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return None
        try:
            vocabulary = self._read()
        except OSError as exc:
            log.warning("vocabulary file not readable (%s); the previous vocabulary stays", type(exc).__name__)
            return None
        except VocabularyError as exc:
            self._stamp = stamp  # logged once; read again when it changes
            log.warning("%s; the previous vocabulary stays", exc)
            return None
        self._stamp = stamp
        if vocabulary == self.vocabulary:
            return None
        self.vocabulary = vocabulary
        return vocabulary


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
    """Hint words in priority order: personal names, extra names, generic terms, personal terms.

    A word listed twice keeps its first place and the personal spelling.
    """
    spelling = {" ".join(entry.text.split()).casefold(): entry.text for entry in vocabulary.entries}
    words = _dedupe([
        *(entry.text for entry in vocabulary.names),
        *extra_names,
        *generic_terms,
        *(entry.text for entry in vocabulary.terms),
    ])
    return [spelling.get(word.casefold(), word) for word in words]


def hints_within_limit(hints: Sequence[str], max_chars: int = HINT_MAX_CHARS) -> tuple[int, int]:
    """(kept, dropped): how many hint words fit ``max_chars``, as ``join_vocabulary`` joins them."""
    out, kept = "", 0
    for word in hints:
        candidate = word if not out else out + ", " + word
        if len(candidate) > max_chars:
            break
        out, kept = candidate, kept + 1
    return kept, len(hints) - kept


def whisper_hints(vocabulary: Vocabulary, extra_names: Iterable[str] = (), generic_terms: Iterable[str] = (),
                  max_chars: int = HINT_MAX_CHARS) -> list[str]:
    """The hints Whisper gets: ``hint_list`` cut at the first word that does not fit ``max_chars``."""
    hints = hint_list(vocabulary, extra_names, generic_terms)
    kept, _ = hints_within_limit(hints, max_chars)
    return hints[:kept]


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


# ---------------------------------------------------------------- adding entries

NEW_FILE_HEADER = "# Personal vocabulary (ignored by Git). Format: vocabulary.example.toml.\n"
ARRAY_INDENT = "    "
# The first table header ends the top level, where ``names`` and ``terms`` live.
TABLE_HEADER = re.compile(r"^[ \t]*\[", re.M)
VARIANTS_HEADER = re.compile(r"^[ \t]*\[[ \t]*variants[ \t]*\][ \t]*(?:#[^\n]*)?$", re.M)
KEY_LINE = re.compile(r"""^[ \t]*("(?:[^"\\\n]|\\.)*"|'[^'\n]*'|[A-Za-z0-9_-]+)[ \t]*=[ \t]*\[""", re.M)


@dataclass(frozen=True)
class Added:
    """What ``add_entries`` did: counts only."""

    added: int
    skipped: int  # already listed
    backup: Path | None  # set when the file had to be rewritten from its data
    vocabulary: Vocabulary = field(repr=False)


def _toml_string(text: str) -> str:
    # A JSON string of printable characters is a valid TOML basic string.
    return json.dumps(text, ensure_ascii=False)


def _string_end(text: str, index: int) -> int:
    """The index just after the TOML string that starts at ``text[index]``."""
    ch = text[index]
    quote = ch * 3 if text.startswith(ch * 3, index) else ch
    end = index + len(quote)
    while True:
        end = text.find(quote, end)
        if end < 0:
            raise ValueError("unterminated string")
        if ch == '"':
            backslashes = end - len(text[:end].rstrip("\\"))
            if backslashes % 2:
                end += 1
                continue
        return end + len(quote)


def _array_end(text: str, start: int) -> tuple[int, list[tuple[int, int]], bool]:
    """For the array opening at ``text[start] == "["``: the index of its ``]``,
    the (start, end) of each element and whether a comma follows the last one."""
    depth, index, items, comma = 0, start, [], False
    nested_start = 0
    while index < len(text):
        ch = text[index]
        if ch == "#":
            index = text.find("\n", index)
            if index < 0:
                break
            continue
        if ch in "\"'":
            end = _string_end(text, index)
            if depth == 1:
                items.append((index, end))
                comma = False
            index = end
            continue
        if ch == "[":
            depth += 1
            if depth == 2:
                nested_start = index
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return index, items, comma
            if depth == 1:
                items.append((nested_start, index + 1))
                comma = False
        elif ch == "," and depth == 1:
            comma = True
        elif not ch.isspace() and depth == 1:
            raise ValueError("unexpected value")
        index += 1
    raise ValueError("unterminated array")


def _insert_items(text: str, open_at: int, values: Sequence[str]) -> str:
    """``text`` with ``values`` appended to the array that opens at ``open_at``, in its layout."""
    close, items, comma = _array_end(text, open_at)
    quoted = [_toml_string(value) for value in values]
    if not items:  # an empty array: one element per line
        return text[:open_at + 1] + "".join(f"\n{ARRAY_INDENT}{item}," for item in quoted) + "\n" + text[close:]
    last_end = items[-1][1]
    if "\n" not in text[open_at:close]:  # on one line: it stays on one line
        return text[:last_end] + "".join(f", {item}" for item in quoted) + text[last_end:]
    line_start = text.rfind("\n", 0, items[-1][0]) + 1
    indent = re.match(r"[ \t]*", text[line_start:]).group()
    line_end = text.find("\n", last_end)
    if line_end < 0 or line_end > close:
        # The closing bracket is on the last element's line: the new lines end before it.
        return text[:last_end] + "".join(f",\n{indent}{item}" for item in quoted) + text[last_end:]
    body = "".join(f"\n{indent}{item}," for item in quoted)
    if not comma:
        text = text[:last_end] + "," + text[last_end:]
        line_end += 1
    return text[:line_end] + body + text[line_end:]


def _top_level_end(text: str) -> int:
    found = TABLE_HEADER.search(text)
    return found.start() if found else len(text)


def _add_to_list(text: str, field_name: str, values: Sequence[str]) -> str:
    top = _top_level_end(text)
    found = re.compile(rf"^[ \t]*{field_name}[ \t]*=[ \t]*\[", re.M).search(text, 0, top)
    if found is not None:
        return _insert_items(text, found.end() - 1, values)
    line = f"{field_name} = [" + ", ".join(_toml_string(v) for v in values) + "]\n"
    if top == len(text):
        return text + ("" if not text or text.endswith("\n") else "\n") + line
    return text[:top] + line + "\n" + text[top:]


def _key_text(raw: str) -> str | None:
    try:
        return next(iter(tomllib.loads(f"{raw} = 0")))
    except (tomllib.TOMLDecodeError, StopIteration):
        return None


def _add_variants(text: str, entry: str, values: Sequence[str]) -> str:
    line = f"{_toml_string(entry)} = [" + ", ".join(_toml_string(v) for v in values) + "]"
    header = VARIANTS_HEADER.search(text)
    if header is None:
        return text + ("" if not text or text.endswith("\n") else "\n") + "\n[variants]\n" + line + "\n"
    start = header.end()
    following = TABLE_HEADER.search(text, start)
    end = following.start() if following else len(text)
    for found in KEY_LINE.finditer(text, start, end):
        if _key_text(found.group(1)) == entry:
            return _insert_items(text, found.end() - 1, values)
    # After the section's last non-blank line.
    body_end = start + len(text[start:end].rstrip())
    return text[:body_end] + "\n" + line + ("" if text[body_end:body_end + 1] == "\n" else "\n") + text[body_end:]


def _render(vocabulary: Vocabulary) -> str:
    def array(entries: Sequence[Entry]) -> str:
        if not entries:
            return "[]"
        return "[\n" + "".join(f"{ARRAY_INDENT}{_toml_string(e.text)},\n" for e in entries) + "]"

    lines = [NEW_FILE_HEADER, f"names = {array(vocabulary.names)}\n", "\n", f"terms = {array(vocabulary.terms)}\n",
             "\n", "[variants]\n"]
    for entry in vocabulary.entries:
        if entry.variants:
            lines.append(f"{_toml_string(entry.text)} = [" + ", ".join(_toml_string(v) for v in entry.variants) + "]\n")
    return "".join(lines)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".vocabulary-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _retry(partial(os.replace, temporary, path))
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _backup(path: Path, raw: bytes, clock: Callable[[], float]) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    for attempt in range(100):
        backup = path.with_name(path.name + f".bak-{stamp}" + (f"-{attempt}" if attempt else ""))
        try:
            with backup.open("xb") as stream:
                stream.write(raw)
            return backup
        except FileExistsError:
            continue
    raise OSError("no free backup name")


def add_entries(path: Path = LOCAL_VOCABULARY, *, terms: Sequence[str] = (), names: Sequence[str] = (),
                variants: Sequence[tuple[str, str]] = (), clock: Callable[[], float] = time.time) -> Added:
    """Add terms, project names and (entry, spoken form) variants to the vocabulary file.

    Raises ``VocabularyError`` (naming the option or field, never a value)
    and leaves the file untouched when the file or a new entry is invalid, a
    variant's entry is not listed, a spoken form belongs to another entry or
    a limit would be passed; ``OSError`` when the file cannot be written.
    """
    path = Path(path)
    try:
        raw: bytes | None = _retry(path.read_bytes)
    except FileNotFoundError:
        raw = None
    except OSError as exc:
        raise VocabularyError(f"vocabulary: {_label(path)} cannot be read ({type(exc).__name__})") from None
    current = parse_text(raw, path) if raw is not None else EMPTY

    # Folded spelling -> the entry field that owns it (an entry owns its variants too).
    owners: dict[str, str] = {}
    for kind in ("names", "terms"):
        for index, entry in enumerate(getattr(current, kind)):
            for spelling in (entry.text, *entry.variants):
                owners[fold(spelling)] = f"{kind}[{index}]"
    new: dict[str, list[str]] = {"names": [], "terms": []}
    skipped = 0
    for option, kind, values in (("--add-name", "names", names), ("--add", "terms", terms)):
        for index, value in enumerate(values):
            spelling = _entry_text(value, f"{option} value {index + 1}")
            if fold(spelling) in owners:
                skipped += 1
                continue
            owners[fold(spelling)] = f"{kind}[{len(getattr(current, kind)) + len(new[kind])}]"
            new[kind].append(spelling)

    spellings = {fold(entry.text): entry.text for entry in current.entries}
    spellings.update({fold(text): text for text in [*new["names"], *new["terms"]]})
    new_variants: dict[str, list[str]] = {}
    for index, (entry_value, spoken_value) in enumerate(variants):
        label = f"--add-variant pair {index + 1}"
        owner_text = spellings.get(fold(_entry_text(entry_value, f"{label} entry")))
        if owner_text is None:
            raise VocabularyError(f"vocabulary: {label} entry is not a listed name or term")
        spoken = _entry_text(spoken_value, f"{label} spoken form")
        holder = owners.get(fold(spoken))
        if holder == owners[fold(owner_text)]:
            skipped += 1
            continue
        if holder is not None:
            raise VocabularyError(f"vocabulary: {label} spoken form repeats {holder}")
        owners[fold(spoken)] = owners[fold(owner_text)]
        new_variants.setdefault(owner_text, []).append(spoken)

    added = len(new["names"]) + len(new["terms"]) + sum(len(spoken) for spoken in new_variants.values())
    if not added:
        return Added(0, skipped, None, current)
    table = {entry.text: list(entry.variants) for entry in current.entries if entry.variants}
    for owner_text, spoken in new_variants.items():
        table[owner_text] = table.get(owner_text, []) + spoken
    expected = parse_vocabulary({
        "names": [entry.text for entry in current.names] + new["names"],
        "terms": [entry.text for entry in current.terms] + new["terms"],
        "variants": table,
    })  # the limits, reported with the field names

    edited: str | None = raw.decode("utf-8") if raw is not None else NEW_FILE_HEADER
    newline = "\r\n" if "\r\n" in edited else "\n"  # edited with "\n", written back in the file's style
    edited = edited.replace("\r\n", "\n")
    try:
        for kind in ("names", "terms"):
            if new[kind]:
                edited = _add_to_list(edited, kind, new[kind])
        for owner_text, spoken in new_variants.items():
            edited = _add_variants(edited, owner_text, spoken)
        if parse_vocabulary(tomllib.loads(edited)) != expected:
            edited = None
    except ValueError:  # an unusual layout; also TOMLDecodeError and VocabularyError
        edited = None
    backup = None
    if edited is None:  # rewrite from the data and keep the old file next to it
        if raw is not None:
            backup = _backup(path, raw, clock)
        edited = _render(expected)
    _write_atomic(path, edited.replace("\n", newline).encode("utf-8"))
    return Added(added, skipped, backup, expected)


# ---------------------------------------------------------------- CLI


def summary_line(path: Path, vocabulary: Vocabulary) -> str:
    """Counts only: entries, and how many hints (personal and generic) Whisper gets."""
    counts = vocabulary.counts()
    hints = hint_list(vocabulary, generic_terms=load_generic_terms())
    kept, dropped = hints_within_limit(hints)
    return (f"{_label(path)}; {counts['names']} names, {counts['terms']} terms, {counts['variants']} variants; "
            f"hints {kept} of {len(hints)} fit the prompt ({HINT_MAX_CHARS} characters), {dropped} dropped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.vocabulary", description=__doc__.splitlines()[0])
    parser.add_argument("--check", nargs="?", const=LOCAL_VOCABULARY, type=Path, metavar="PATH",
                        help="validate PATH (default: local/vocabulary.toml); prints counts only")
    parser.add_argument("--add", action="append", default=[], metavar="TERM", help="add a technical term")
    parser.add_argument("--add-name", action="append", default=[], metavar="NAME", help="add a project name")
    parser.add_argument("--add-variant", action="append", default=[], nargs=2, metavar=("ENTRY", "SPOKEN"),
                        help="add a spoken or misheard form of a listed name or term")
    parser.add_argument("--file", type=Path, default=LOCAL_VOCABULARY,
                        help="file the --add options edit (default: local/vocabulary.toml)")
    args = parser.parse_args(argv)
    adding = bool(args.add or args.add_name or args.add_variant)
    if adding == (args.check is not None):
        parser.print_usage(sys.stderr)
        return 2
    if adding:
        try:
            result = add_entries(args.file, terms=args.add, names=args.add_name,
                                 variants=[tuple(pair) for pair in args.add_variant])
        except VocabularyError as exc:
            print(f"{exc}; nothing changed", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"vocabulary: {_label(args.file)} not written ({type(exc).__name__}); nothing changed",
                  file=sys.stderr)
            return 1
        note = f"; rewritten, previous file kept as {result.backup.name}" if result.backup else ""
        print(f"vocabulary: {result.added} added, {result.skipped} already listed{note} "
              f"({summary_line(args.file, result.vocabulary)})")
        return 0
    if not args.check.is_file():
        print(f"vocabulary: {_label(args.check)} not found", file=sys.stderr)
        return 2
    try:
        vocabulary = load_vocabulary(args.check)
    except VocabularyError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"vocabulary: OK ({summary_line(args.check, vocabulary)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
