"""Decoding hints of mouse 5 into Claude Code chosen by what has been heard.

A ``HeardHints`` is the hint source of one mouse 5 session in Claude Code
with a detected project and context pack (``quill.app.project_hints``): the
streaming session (``quill.streaming.Session``) calls it with the text heard
so far (committed and tentative words) after each partial and decodes the
later windows with the hints it returns.

The hints are today's mouse 5 hints (``quill.whisper.project_terms``: the
project name, its distinctive pack terms, then the others in the pack's
relevance order, at most ``PROJECT_HINT_MAX_CHARS``, inside the whole
``HINT_MAX_CHARS`` budget of ``whisper_hints``), except that the pack terms
that sound like something already heard come first. A term is heard
(``HeardMatcher``) when its sound key (``quill.autorewrite.sound_key``)
matches the joined sound key of a span of 1 to ``MAX_SPAN_WORDS`` heard
words, joined only by spaces or hyphens, within ``max_distance`` edits
(``quill.vocabulary.distance``): a term whose sound key has under
``EXACT_BELOW`` letters (so every term of under four folded letters) only on
an exact match, any other with the same first letter. A
multi-part term (``Nimbus-Deck``, ``NimbusDeck``) thus matches "nimbos
deck". The closest terms come first, ties in the pack's order. With nothing
heard the hints are exactly today's.

A source remembers what its session has heard: a term heard in one partial
stays heard, with the fewest edits it has matched, even when a later partial
words that span otherwise. The hints so change only when a new term is heard
or a term is heard closer, never back and forth as the tentative words of the
partials change: each change makes the session drop its speculative finals,
so the final may decode again after the release.

Matching is incremental: each distinct span is compared with the terms once
(a bounded memo), so a partial only costs its new words. The hints depend
only on the texts the source was given, in order, so the same sequence gives
the same hints. Nothing here logs; terms and text never leave the object.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence

from quill.autorewrite import sound_key
from quill.vocabulary import distance as edit_distance
from quill.vocabulary import whisper_hints
from quill.whisper import SessionHints, project_terms, session_hints, spoken_term

MAX_SPAN_WORDS = 3
EXACT_BELOW = 4  # terms with fewer sound-key letters match only exactly
TWO_EDITS_FROM = 8  # sound-key letters from which two edits are allowed (one below)
MAX_MEMO = 4096  # remembered spans; the memo starts again when full
# A heard word: letters or digits, with inner hyphens or apostrophes.
WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
# What may separate the words of one span.
JOINER = re.compile(r"^[\s-]+$")


def max_distance(key: str) -> int:
    """Edits allowed between a term's sound key and a heard span's."""
    if len(key) < EXACT_BELOW:
        return 0
    return 2 if len(key) >= TWO_EDITS_FROM else 1


def span_keys(heard: str, max_words: int = MAX_SPAN_WORDS) -> list[str]:
    """Sound keys of every span of 1 to ``max_words`` heard words, in order, without repeats."""
    keys: list[str] = []
    seen: set[str] = set()
    words = list(WORD.finditer(heard or ""))
    for first in range(len(words)):
        key = ""
        for last in range(first, min(first + max_words, len(words))):
            if last > first and not JOINER.match(heard[words[last - 1].end():words[last].start()]):
                break
            key += sound_key(words[last].group())
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


class HeardMatcher:
    """Pack terms that sound like a span of the heard text (see the module docstring)."""

    def __init__(self, terms: Sequence[str], *, distance: Callable[[str, str, int], int] = edit_distance) -> None:
        self.distance = distance
        # (pack position, term, sound key) of every spoken term, first spelling of a key only.
        self.terms: list[tuple[int, str, str]] = []
        keys: set[str] = set()
        for position, term in enumerate(terms):
            if not isinstance(term, str) or not term.strip() or not spoken_term(term):
                continue
            key = sound_key(term)
            if key and key not in keys:
                keys.add(key)
                self.terms.append((position, " ".join(term.split()), key))
        self._memo: dict[str, tuple[tuple[int, int], ...]] = {}
        self._lock = threading.Lock()

    def _match(self, span: str) -> tuple[tuple[int, int], ...]:
        """(term index, edits) of the terms that ``span`` matches."""
        found = []
        for index, (_, _, key) in enumerate(self.terms):
            bound = max_distance(key)
            if abs(len(key) - len(span)) > bound:
                continue
            if span == key:
                found.append((index, 0))
            elif bound and span[0] == key[0]:
                edits = self.distance(span, key, bound)
                if edits <= bound:
                    found.append((index, edits))
        return tuple(found)

    def matches(self, heard: str) -> list[str]:
        """The heard terms, closest first, ties in the pack's order."""
        return self.ranked(self.closest(heard))

    def closest(self, heard: str) -> dict[int, int]:
        """The fewest edits with which each heard term (by index) matched a span of ``heard``."""
        best: dict[int, int] = {}
        for span in span_keys(heard):
            with self._lock:
                found = self._memo.get(span)
            if found is None:
                found = self._match(span)
                with self._lock:
                    if len(self._memo) >= MAX_MEMO:
                        self._memo.clear()
                    self._memo[span] = found
            for index, edits in found:
                if edits < best.get(index, edits + 1):
                    best[index] = edits
        return best

    def ranked(self, best: dict[int, int]) -> list[str]:
        """The terms of ``best`` (index -> edits), closest first, ties in the pack's order."""
        ranked = sorted(best, key=lambda index: (best[index], self.terms[index][0]))
        return [self.terms[index][1] for index in ranked]


class HeardHints:
    """The hint source of one mouse 5 session: heard text -> ``SessionHints`` (None: the vocabulary hints).

    ``initial`` (also ``prompt``, ``hotwords`` and ``language``) are the hints
    with nothing heard: today's project hints. Every term heard so far stays
    heard (see the module docstring), so one source serves one session.
    """

    def __init__(self, project: str, terms: Sequence[str], vocabulary: object, generic_terms: Sequence[str] = (),
                 *, matcher: HeardMatcher | None = None) -> None:
        self.project = project
        self.terms = tuple(terms)
        self.vocabulary = vocabulary
        self.generic_terms = tuple(generic_terms)
        self.today = whisper_hints(vocabulary, (), self.generic_terms)
        self.matcher = matcher if matcher is not None else HeardMatcher(self.terms)
        self._heard: dict[int, int] = {}  # every term heard in this session (by index): the fewest edits
        self._lock = threading.Lock()  # asked from the hint thread and the transcription worker
        self.initial = self("")

    def words(self, heard: str) -> list[str]:
        """The hint words after ``heard``: the vocabulary hints with the project part placed after the names."""
        found = self.matcher.closest(heard) if heard else {}
        with self._lock:
            for index, edits in found.items():
                if edits < self._heard.get(index, edits + 1):
                    self._heard[index] = edits
            first = self.matcher.ranked(self._heard)
        part = project_terms(self.project, self.terms, self.today, first=first)
        return whisper_hints(self.vocabulary, part, self.generic_terms)

    def __call__(self, heard: str) -> SessionHints | None:
        return session_hints(self.words(heard))

    @property
    def prompt(self) -> str | None:
        return self.initial.prompt if self.initial is not None else None

    @property
    def hotwords(self) -> str | None:
        return self.initial.hotwords if self.initial is not None else None

    @property
    def language(self) -> str | None:
        return self.initial.language if self.initial is not None else None
