"""Learning from corrections: derive replacements, count them, apply the active ones.

A correction is the text Quill typed next to the text the user wanted. It
reaches this module in two ways: the correction key (the user selects the
corrected text and presses the key; the selection is read through a
clipboard save/copy/restore, ``quill.clipboard.copy_selection``) and
manual-edit detection (``quill.edits`` mirrors the typed span from the
keyboard). Only the derived replacements are kept, never keystrokes.

``derive`` aligns the two texts word by word (case-insensitive) and turns
each run of changed words into one replacement, for example ``ram`` ->
``run`` or ``pai tone`` -> ``Python``. It keeps nothing when the change is a
rewrite rather than a correction: a side longer than ``MAX_PHRASE_WORDS``, a
pure insertion or deletion, more than ``MAX_SPANS`` changed runs, or fewer
than half of the words unchanged. It also keeps nothing when both sides are
only function words (articles, prepositions, pronouns, conjunctions: ``dos``
-> ``do``, ``um`` -> ``o``): the right one depends on the sentence, so a
whole-word replacement would be right in one text and wrong in the next.
With ``partial=True`` the corrected text may be a selection of part of the
dictated text; a selection that could sit in more than one place (a lone
corrected word with no unchanged word around it) is not located, so
nothing is learned from it.

The product rule (``Corrections``): a replacement seen in
``AUTO_APPLY_AFTER`` (two) distinct dictations becomes active and is applied
to later text as whole words, case-preserving; the same dictation seen twice
counts once. Replacements that conflict (one source with two different
targets, or a pair and its reverse) are never applied automatically until
the user approves one in the review (``python -m quill.review``). A deleted
replacement is remembered as rejected and not learned again.

Storage (``CorrectionStore``): ``local/corrections.json`` (ignored by Git),
versioned by ``schema``, written atomically (temporary file and replace). A
corrupt or unreadable-schema file is renamed to
``corrections.json.corrupt-<time>`` and replaced by an empty one; loading
never raises. Logs hold counts and reasons only, never the text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("quill.corrections")

SCHEMA = 1
AUTO_APPLY_AFTER = 2
MAX_PHRASE_WORDS = 3
MAX_SPANS = 5
MIN_UNCHANGED = 0.5
MAX_ENTRIES = 1000
MAX_DICTATIONS = 10
MAX_TEXT = 200
MAX_SELECTION = 4000

WORD = re.compile(r"\w+(?:['’-]\w+)*")
WHITESPACE = re.compile(r"\s+")
DICTATION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SENTENCE_END = ".!?\n"
# Words whose right form depends on the sentence (European Portuguese and English).
FUNCTION_WORDS = frozenset("""
o a os as um uma uns umas de do da dos das d em no na nos nas num numa nuns numas ao aos à às
por pelo pela pelos pelas para pra com sem sob sobre entre até desde e ou mas nem que se como
quando onde porque pois eu tu ele ela nós vós eles elas me te lhe lhes vos mim ti si
meu minha meus minhas teu tua teus tuas seu sua seus suas nosso nossa nossos nossas
este esta estes estas esse essa esses essas aquele aquela aqueles aquelas isto isso aquilo
deste desta destes destas desse dessa desses dessas neste nesta nestes nestas nesse nessa
nesses nessas daquele daquela naquele naquela lo la los las
the an of to in on at for and or but if is it its this that these those with by from as
""".split())

ACTIVE = "active"
PENDING = "pending"
CONFLICT = "conflict"


class CorrectionsError(ValueError):
    """The corrections file does not follow the schema; the message never holds its text."""


def fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def new_dictation_id() -> str:
    """An opaque id for one dictation: random, never derived from the text."""
    return uuid.uuid4().hex


def now_iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


# ---------------------------------------------------------------- derive


@dataclass(frozen=True)
class Word:
    text: str = field(repr=False)
    start: int
    end: int


def words(text: str) -> list[Word]:
    return [Word(match.group(0), match.start(), match.end()) for match in WORD.finditer(text)]


@dataclass(frozen=True)
class Replacement:
    """One derived change: the dictated words and the corrected words."""

    source: str = field(repr=False)
    target: str = field(repr=False)

    @property
    def key(self) -> tuple[str, ...]:
        return phrase_key(self.source)


def phrase_key(text: str) -> tuple[str, ...]:
    return tuple(fold(word.text) for word in words(text))


Alignment = list[tuple[int | None, int | None]]


def _align(a: Sequence[str], b: Sequence[str], partial: bool) -> list[Alignment]:
    """Minimum-cost word alignments of ``a`` (dictated) and ``b`` (corrected).

    Each alignment is a list of (index in a, index in b) pairs; None marks a
    gap. Without ``partial`` there is exactly one. With ``partial`` the words
    of ``a`` before and after the aligned window cost nothing and are left
    out, and there is one alignment per window end that reaches the minimum
    cost, last end first: a selection can fit more than one place. Ties
    inside an alignment prefer a match or substitution, then a deletion.
    """
    n, m = len(a), len(b)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    move = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        cost[i][0] = 0 if partial else i
        move[i][0] = "start" if partial or i == 0 else "del"
    for j in range(1, m + 1):
        cost[0][j] = j
        move[0][j] = "ins"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            same = a[i - 1] == b[j - 1]
            options = (
                (cost[i - 1][j - 1] + (0 if same else 1), "diag"),
                (cost[i - 1][j] + 1, "del"),
                (cost[i][j - 1] + 1, "ins"),
            )
            cost[i][j], move[i][j] = min(options, key=lambda item: item[0])
    ends = [n]
    if partial:
        best = min(cost[i][m] for i in range(n + 1))
        ends = [i for i in range(n, -1, -1) if cost[i][m] == best]
    paths: list[Alignment] = []
    for end in ends:
        path: Alignment = []
        i, j = end, m
        while i > 0 or j > 0:
            step = move[i][j]
            if step == "start":
                break
            if step == "diag":
                path.append((i - 1, j - 1))
                i, j = i - 1, j - 1
            elif step == "del":
                path.append((i - 1, None))
                i -= 1
            else:
                path.append((None, j - 1))
                j -= 1
        path.reverse()
        paths.append(path)
    return paths


def _sentence_initial(text: str, start: int) -> bool:
    before = text[:start].rstrip()
    return not before or before[-1] in SENTENCE_END


def _case_neutral(target: str, initial: bool) -> str:
    """A sentence-start capital says nothing about the word: store it lowercase."""
    rest = target[1:]
    if initial and target[:1].isupper() and not any(ch.isupper() for ch in rest):
        return target[:1].lower() + rest
    return target


def _function_words(items: Iterable[Word]) -> bool:
    return all(fold(word.text) in FUNCTION_WORDS for word in items)


def derive(dictated: str, corrected: str, *, partial: bool = False, case_sensitive: bool = True,
           strict: bool = True) -> list[Replacement]:
    """Word and phrase replacements from ``dictated`` to ``corrected``; [] for a rewrite.

    ``strict=False`` drops the rewrite and function-word guards (the span
    count, the unchanged share and the sentence-dependent words); the
    benchmark uses it to list every error, never to learn.

    With ``partial`` a selection that fits several places of the dictation
    equally well and would give different replacements there (for example
    one corrected word that shares nothing with the dictation) is not
    located: nothing is learned rather than a guess. The one exception is a
    selection at least as long as the dictation that fits the whole of it.
    """
    a, b = words(dictated), words(corrected)
    if not a or not b:
        return []
    paths = _align([fold(w.text) for w in a], [fold(w.text) for w in b], partial)
    found: list[list[Replacement]] = []
    for path in paths:
        replacements = _derive_path(dictated, corrected, a, b, path, case_sensitive, strict)
        if replacements and replacements not in found:
            found.append(replacements)
    if len(found) <= 1:
        return found[0] if found else []
    whole = paths[0]
    if len(a) <= len(b) and sum(1 for i, _ in whole if i is not None) == len(a):
        # paths[0] is the last end, the dictation's end: the selection covers all of it.
        return _derive_path(dictated, corrected, a, b, whole, case_sensitive, strict)
    return []


def _derive_path(dictated: str, corrected: str, a: list[Word], b: list[Word], path: Alignment,
                 case_sensitive: bool, strict: bool) -> list[Replacement]:
    spans: list[tuple[list[int], list[int]]] = []
    current: tuple[list[int], list[int]] | None = None
    unchanged = 0
    def same(x: Word, y: Word) -> bool:
        if fold(x.text) != fold(y.text):
            return False
        if x.text == y.text or not case_sensitive:
            return True
        # Only a sentence-start capital differs: not a correction.
        initial = _sentence_initial(dictated, x.start) or _sentence_initial(corrected, y.start)
        return initial and x.text[1:] == y.text[1:]

    for i, j in path:
        if i is not None and j is not None and same(a[i], b[j]):
            unchanged += 1
            current = None
            continue
        if current is None:
            current = ([], [])
            spans.append(current)
        if i is not None:
            current[0].append(i)
        if j is not None:
            current[1].append(j)
    aligned = sum(1 for i, _ in path if i is not None)
    # One changed word is always allowed, so a one-word dictation can be corrected.
    if not spans:
        return []
    if strict and (len(spans) > MAX_SPANS or unchanged < MIN_UNCHANGED * max(aligned, len(b)) - 1):
        return []
    found: list[Replacement] = []
    for src, tgt in spans:
        if not src or not tgt or len(src) > MAX_PHRASE_WORDS or len(tgt) > MAX_PHRASE_WORDS:
            continue
        if strict and _function_words(a[i] for i in src) and _function_words(b[j] for j in tgt):
            continue  # grammar that depends on the sentence, not a word to replace everywhere
        first, last = a[src[0]], a[src[-1]]
        source = " ".join(a[i].text for i in src)
        # A line break or tab between corrected words is a space in a replacement.
        target = WHITESPACE.sub(" ", corrected[b[tgt[0]].start:b[tgt[-1]].end])
        # A capital at a sentence start (typed or corrected) is not part of the word.
        initial = _sentence_initial(dictated, first.start) or _sentence_initial(corrected, b[tgt[0]].start)
        target = _case_neutral(target, initial)
        if source == target or (initial and _case_neutral(source, True) == target):
            continue
        if len(source) > MAX_TEXT or len(target) > MAX_TEXT or last.end < first.start:
            continue
        if not _storable(source) or not _storable(target):
            continue
        found.append(Replacement(source, target))
    return found


# ---------------------------------------------------------------- the rule


@dataclass
class Entry:
    """One learned replacement and the distinct dictations it was seen in."""

    source: str = field(repr=False)
    target: str = field(repr=False)
    dictations: list[str] = field(default_factory=list, repr=False)
    approved: bool = False
    first_seen: str = ""
    last_seen: str = ""

    @property
    def key(self) -> tuple[str, ...]:
        return phrase_key(self.source)

    @property
    def target_key(self) -> tuple[str, ...]:
        return phrase_key(self.target)

    @property
    def seen(self) -> int:
        return len(self.dictations)


@dataclass(frozen=True)
class LearnResult:
    derived: int = 0
    counted: int = 0
    activated: int = 0
    rejected: int = 0


def _case_like(occurrence: str, target: str) -> str:
    letters = [ch for ch in occurrence if ch.isalpha()]
    if len(letters) > 1 and all(ch.isupper() for ch in letters):
        return target.upper()
    if occurrence[:1].isupper() and target[:1].islower():
        return target[:1].upper() + target[1:]
    return target


class Corrections:
    """The learned replacements, their status and the automatic application."""

    def __init__(self, created: str, last_review: str | None = None, entries: Iterable[Entry] = (),
                 rejected: Iterable[tuple[tuple[str, ...], tuple[str, ...]]] = ()) -> None:
        self.created = created
        self.last_review = last_review
        self.entries: list[Entry] = list(entries)
        self.rejected: set[tuple[tuple[str, ...], tuple[str, ...]]] = set(rejected)

    @classmethod
    def empty(cls, now: float) -> Corrections:
        return cls(now_iso(now))

    # -- status

    def statuses(self) -> dict[int, str]:
        """Status of every entry, by position: active, pending or conflict."""
        by_key: dict[tuple[str, ...], list[int]] = {}
        for index, entry in enumerate(self.entries):
            by_key.setdefault(entry.key, []).append(index)
        pairs = {(entry.key, entry.target_key) for entry in self.entries}
        result: dict[int, str] = {}
        for key, indexes in by_key.items():
            approved = [i for i in indexes if self.entries[i].approved]
            for i in indexes:
                entry = self.entries[i]
                if approved:
                    result[i] = ACTIVE if i == approved[-1] else CONFLICT
                elif len(indexes) > 1 or (entry.key != entry.target_key and (entry.target_key, entry.key) in pairs):
                    result[i] = CONFLICT
                else:
                    result[i] = ACTIVE if entry.seen >= AUTO_APPLY_AFTER else PENDING
        return result

    def active(self) -> list[Entry]:
        statuses = self.statuses()
        return [entry for index, entry in enumerate(self.entries) if statuses[index] == ACTIVE]

    def counts(self) -> dict[str, int]:
        statuses = list(self.statuses().values())
        return {"active": statuses.count(ACTIVE), "pending": statuses.count(PENDING),
                "conflict": statuses.count(CONFLICT), "rejected": len(self.rejected)}

    # -- learning

    def learn(self, dictation_id: str, replacements: Iterable[Replacement], now: float) -> LearnResult:
        """Count each replacement once for this dictation; returns counts only."""
        if not DICTATION_ID.match(dictation_id):
            raise ValueError("invalid dictation id")
        before = {id(entry) for entry in self.active()}
        derived = counted = rejected = 0
        stamp = now_iso(now)
        seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
        for replacement in replacements:
            derived += 1
            pair = (replacement.key, phrase_key(replacement.target))
            if not pair[0] or pair in seen or not _storable(replacement.source) or not _storable(replacement.target):
                continue
            seen.add(pair)
            if pair in self.rejected:
                rejected += 1
                continue
            entry = next((e for e in self.entries if (e.key, e.target_key) == pair), None)
            if entry is None:
                entry = Entry(replacement.source, replacement.target, first_seen=stamp)
                self.entries.append(entry)
            else:
                entry.target = replacement.target  # the latest spelling of the same target
            if dictation_id not in entry.dictations:
                entry.dictations = (entry.dictations + [dictation_id])[-MAX_DICTATIONS:]
                counted += 1
            entry.last_seen = stamp
        self._trim()
        activated = sum(1 for entry in self.active() if id(entry) not in before)
        return LearnResult(derived, counted, activated, rejected)

    def _trim(self) -> None:
        if len(self.entries) <= MAX_ENTRIES:
            return
        statuses = self.statuses()
        removable = sorted((i for i in statuses if not self.entries[i].approved and statuses[i] != ACTIVE),
                           key=lambda i: self.entries[i].last_seen)
        # Active and approved entries go last, oldest first, so the file stays loadable.
        kept = sorted(set(statuses) - set(removable), key=lambda i: self.entries[i].last_seen)
        drop = set((removable + kept)[: len(self.entries) - MAX_ENTRIES])
        self.entries = [entry for i, entry in enumerate(self.entries) if i not in drop]

    # -- review

    def approve(self, entry: Entry) -> None:
        """Active from now on, even if seen once; wins over its conflicts."""
        for other in self.entries:
            if other is not entry and (other.key == entry.key or (other.key, other.target_key) == (entry.target_key, entry.key)):
                other.approved = False
        entry.approved = True

    def delete(self, entry: Entry) -> None:
        self.entries = [e for e in self.entries if e is not entry]
        self.rejected.add((entry.key, entry.target_key))

    def mark_reviewed(self, now: float) -> None:
        self.last_review = now_iso(now)

    # -- application

    def apply(self, text: str) -> tuple[str, int]:
        """``text`` with the active replacements applied (whole words, case-preserving)."""
        table = {entry.key: entry.target for entry in self.active()}
        if not table or not text:
            return text, 0
        longest = max(len(key) for key in table)
        found = words(text)
        out: list[str] = []
        position = index = applied = 0
        while index < len(found):
            for size in range(min(longest, len(found) - index), 0, -1):
                span = found[index:index + size]
                if any(text[x.end:y.start].strip() for x, y in zip(span, span[1:])):
                    continue  # never across punctuation
                target = table.get(tuple(fold(w.text) for w in span))
                if target is None:
                    continue
                occurrence = text[span[0].start:span[-1].end]
                out.append(text[position:span[0].start])
                out.append(_case_like(occurrence, target))
                position = span[-1].end
                index += size
                applied += 1
                break
            else:
                index += 1
        out.append(text[position:])
        return "".join(out), applied

    # -- serialization

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "created": self.created,
            "last_review": self.last_review,
            "entries": [
                {"source": e.source, "target": e.target, "dictations": list(e.dictations), "approved": e.approved,
                 "first_seen": e.first_seen, "last_seen": e.last_seen}
                for e in self.entries
            ],
            "rejected": [{"source": list(src), "target": list(tgt)} for src, tgt in sorted(self.rejected)],
        }

    @classmethod
    def from_json(cls, data: object) -> Corrections:
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise CorrectionsError("unsupported schema")
        unknown = set(data) - {"schema", "created", "last_review", "entries", "rejected"}
        if unknown:
            raise CorrectionsError("unknown fields")
        created = _stamp(data.get("created"), "created")
        last_review = None if data.get("last_review") is None else _stamp(data.get("last_review"), "last_review")
        raw_entries, raw_rejected = data.get("entries"), data.get("rejected")
        if not isinstance(raw_entries, list) or not isinstance(raw_rejected, list) or len(raw_entries) > MAX_ENTRIES:
            raise CorrectionsError("entries and rejected must be lists")
        entries = [_entry(item, index) for index, item in enumerate(raw_entries)]
        rejected = set()
        for index, item in enumerate(raw_rejected):
            if not isinstance(item, dict) or set(item) != {"source", "target"}:
                raise CorrectionsError(f"rejected[{index}] is invalid")
            rejected.add((_key(item["source"], f"rejected[{index}]"), _key(item["target"], f"rejected[{index}]")))
        return cls(created, last_review, entries, rejected)


def _stamp(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise CorrectionsError(f"{name} must be a timestamp")
    try:
        parse_iso(value)
    except (ValueError, OverflowError, OSError):
        raise CorrectionsError(f"{name} must be a timestamp") from None
    return value


def _storable(text: str) -> bool:
    """``text`` passes the loader's checks, so a saved entry never wipes the file."""
    return (bool(text.strip()) and len(text) <= MAX_TEXT and 1 <= len(words(text)) <= MAX_PHRASE_WORDS
            and not any(unicodedata.category(ch) == "Cc" for ch in text))


def _phrase(value: object, name: str) -> str:
    if not isinstance(value, str) or not _storable(value):
        raise CorrectionsError(f"{name} is invalid")
    return value


def _key(value: object, name: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or not 1 <= len(value) <= MAX_PHRASE_WORDS
            or not all(isinstance(item, str) and 0 < len(item) <= MAX_TEXT for item in value)):
        raise CorrectionsError(f"{name} is invalid")
    return tuple(value)


def _entry(item: object, index: int) -> Entry:
    name = f"entries[{index}]"
    fields = {"source", "target", "dictations", "approved", "first_seen", "last_seen"}
    if not isinstance(item, dict) or set(item) != fields:
        raise CorrectionsError(f"{name} is invalid")
    dictations = item["dictations"]
    if (not isinstance(dictations, list) or len(dictations) > MAX_DICTATIONS
            or not all(isinstance(d, str) and DICTATION_ID.match(d) for d in dictations)
            or len(set(dictations)) != len(dictations)):
        raise CorrectionsError(f"{name}.dictations is invalid")
    if not isinstance(item["approved"], bool):
        raise CorrectionsError(f"{name}.approved is invalid")
    source, target = _phrase(item["source"], f"{name}.source"), _phrase(item["target"], f"{name}.target")
    return Entry(source, target, list(dictations), item["approved"],
                 _stamp(item["first_seen"], f"{name}.first_seen"), _stamp(item["last_seen"], f"{name}.last_seen"))


# ---------------------------------------------------------------- storage


class CorrectionStore:
    """``local/corrections.json``: atomic writes; a corrupt file is backed up and replaced."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.writable = True
        self._stamp: tuple[int, int] | None = None

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def changed(self) -> bool:
        """The file changed on disk since this store last read or wrote it."""
        return self._file_stamp() != self._stamp

    def load(self) -> Corrections:
        """The stored corrections; never raises."""
        now = self.clock()
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self._stamp = None
            self.writable = True  # nothing left that a save could overwrite unread
            return Corrections.empty(now)
        except OSError as exc:
            # Not readable: work in memory and never overwrite what could not be read.
            log.error("corrections file not readable (%s); learning is not saved", type(exc).__name__)
            self.writable = False
            return Corrections.empty(now)
        try:
            corrections = Corrections.from_json(json.loads(raw.decode("utf-8")))
        except (ValueError, OverflowError, OSError, RecursionError) as exc:
            # ValueError covers bad UTF-8, bad JSON, over-long integers and CorrectionsError.
            reason = exc.args[0] if isinstance(exc, CorrectionsError) else type(exc).__name__
            self._backup(reason)
            corrections = Corrections.empty(now)
            self.save(corrections)
            return corrections
        self._stamp = self._file_stamp()
        self.writable = True  # read again after a transient failure: saving is safe again
        return corrections

    def _backup(self, reason: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        for attempt in range(100):
            suffix = f".corrupt-{stamp}" + (f"-{attempt}" if attempt else "")
            backup = self.path.with_name(self.path.name + suffix)
            if backup.exists():
                continue
            try:
                os.replace(self.path, backup)
            except OSError as exc:
                log.error("corrupt corrections file could not be backed up (%s); learning is not saved", type(exc).__name__)
                self.writable = False
                return
            log.warning("corrections file was invalid (%s); backed up as %s and replaced", reason, backup.name)
            return
        log.error("corrupt corrections file could not be backed up; learning is not saved")
        self.writable = False

    def save(self, corrections: Corrections) -> bool:
        """Write atomically; False (logged) on failure, never raises."""
        if not self.writable:
            return False
        data = json.dumps(corrections.to_json(), ensure_ascii=False, indent=2).encode("utf-8")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=".corrections-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            log.error("corrections not saved (%s)", type(exc).__name__)
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self._stamp = self._file_stamp()
        return True


# ---------------------------------------------------------------- product wiring


@dataclass(frozen=True)
class Dictation:
    """The last text Quill typed: an opaque id, the text and its target window."""

    id: str
    text: str = field(repr=False)
    target: int
    at: float


class Learner:
    """Thread-safe front of the store: apply the active replacements and learn new ones.

    It reloads the file when it changed on disk (for example after a review)
    before learning, so a review and the app never undo each other.
    """

    def __init__(self, store: CorrectionStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        self._lock = threading.Lock()
        self.corrections = store.load()

    def _refresh(self) -> None:
        if self.store.changed():
            self.corrections = self.store.load()

    def apply(self, text: str) -> str:
        with self._lock:
            self._refresh()
            result, applied = self.corrections.apply(text)
        if applied:
            log.info("applied %d learned corrections", applied)
        return result

    def learn(self, dictation: Dictation, corrected: str, *, partial: bool, source: str) -> LearnResult:
        replacements = derive(dictation.text, corrected, partial=partial)
        if not replacements:
            log.info("%s: no replacement derived", source)
            return LearnResult()
        with self._lock:
            self._refresh()
            result = self.corrections.learn(dictation.id, replacements, self.clock())
            self.store.save(self.corrections)
        log.info("%s: %d replacements derived, %d counted, %d activated, %d rejected",
                 source, result.derived, result.counted, result.activated, result.rejected)
        return result


# Outcomes of the correction key.
LEARNED = "learned"
NO_DICTATION = "no_dictation"
OTHER_WINDOW = "other_window"
NO_SELECTION = "no_selection"
NOTHING_LEARNED = "nothing_learned"
CLIPBOARD_FAILED = "clipboard_failed"


class CorrectionKey:
    """The correction key: read the selected corrected text and learn from it.

    ``read_selection`` returns the selected text or None (see
    ``quill.clipboard.copy_selection``; it restores the clipboard on every
    path). ``foreground`` gives the current foreground window. Only the last
    dictation is compared, and only in the window it was typed into.
    """

    def __init__(self, learner: Learner, read_selection: Callable[[], str | None],
                 foreground: Callable[[], int]) -> None:
        self.learner = learner
        self.read_selection = read_selection
        self.foreground = foreground
        self.last: Dictation | None = None

    def remember(self, dictation: Dictation) -> None:
        self.last = dictation

    def press(self) -> str:
        last = self.last
        if last is None:
            log.info("correction key: no dictation to correct")
            return NO_DICTATION
        if self.foreground() != last.target:
            log.info("correction key: foreground is not the dictation's window")
            return OTHER_WINDOW
        try:
            selected = self.read_selection()
        except OSError as exc:
            log.warning("correction key: selection not read (%s)", type(exc).__name__)
            return CLIPBOARD_FAILED
        if not selected or not selected.strip() or len(selected) > MAX_SELECTION:
            log.info("correction key: no usable selection")
            return NO_SELECTION
        result = self.learner.learn(last, selected, partial=True, source="correction key")
        return LEARNED if result.counted else NOTHING_LEARNED
