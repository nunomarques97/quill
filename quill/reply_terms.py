"""A bounded term list and the shape of a Claude Code reply, derived from its visible text.

``derive(text)`` turns the visible text of Claude's last reply (see
``quill.claude_reply``) into a ``ReplyContext``: the words the user is likely to
say back, as candidates for decoding hints and for sound-close replacements in
the correction step, plus whether the reply asks something. The text itself is
never kept.

- Terms: the head words of the options the reply offers, then identifiers and
  file names, then content words. Within each kind, later paragraphs come first
  (the end of a reply is what the user answers), in reading order inside a
  paragraph. Terms are deduplicated by ``quill.autorewrite.sound_key`` and
  bounded by ``max_terms`` and ``max_chars`` (joined by ", ").
- Fenced code blocks and inline code give identifiers only: CamelCase,
  snake_case and hyphenated names and file names. URLs and paths give their file
  base name only. Markdown markers, links targets, images and HTML tags are
  stripped.
- Dropped: function words (``quill.corrections.FUNCTION_WORDS``), hesitations,
  number words, polarity words ("nunca", "nada"), words under
  ``MIN_TERM_CHARS`` letters and any term holding a digit, so numbers and
  negations stay protected.
- Options: numbered or labelled choices at the start of a line ("1.", "2)",
  "a)", "B:", "Opção A", "Option 2:"). The last list of at least two such
  choices is kept, with its labels and up to ``MAX_HEAD_WORDS`` head words each.
- ``asks``: the last prose paragraph holds a question mark, or options are
  offered.

The same input always gives the same output. ``repr`` shows counts only.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from quill.autorewrite import COMMON_NUMBER_WORDS, COMMON_POLARITY, sound_key
from quill.cleanup import HESITATIONS
from quill.corrections import FUNCTION_WORDS

__all__ = ["DEFAULT_MAX_CHARS", "DEFAULT_MAX_TERMS", "EMPTY", "ReplyContext", "ReplyOption", "derive"]

DEFAULT_MAX_TERMS = 40
DEFAULT_MAX_CHARS = 400  # the terms joined by ", "
MAX_INPUT_CHARS = 20000  # a longer text is cut to its end
MIN_TERM_CHARS = 4  # letters of a term
MAX_TERM_CHARS = 48  # a longer token is not a word anyone says
MAX_OPTIONS = 9
MAX_HEAD_WORDS = 3  # words of an option kept as its head
SEPARATOR = ", "

# The kinds of term after the options' head words, in the order they are listed.
IDENTIFIER = 0
CONTENT = 1

DROPPED = FUNCTION_WORDS | HESITATIONS | COMMON_NUMBER_WORDS | COMMON_POLARITY
FILE_EXTENSIONS = frozenset("""
md markdown txt rst toml json jsonl yaml yml ini cfg conf env lock log csv tsv xml html htm css scss
py pyi ipynb js mjs cjs jsx ts tsx vue svelte rs go java kt kts cs fs vb c h cc cpp hpp swift rb php
pl lua sh bash zsh psm bat cmd sql wav pdf png jpg jpeg svg gif ico zip gz exe dll
""".split())

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_URL = re.compile(r"<?(?:(?:https?|ftp|file)://|www\.)[^\s<>()]*[^\s<>().,;:!?'\"]>?", re.IGNORECASE)
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_EMPHASIS = re.compile(r"\*\*|__|~~|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)")
_LINE_MARK = re.compile(r"^\s{0,3}(?:#{1,6}\s+|>\s*)+")
_BULLET = re.compile(r"^\s*[-*+•]\s+")
_RULE = re.compile(r"^\s{0,3}(?:[-*_=]\s*){3,}$")
_TABLE_RULE = re.compile(r"^[\s|:-]*-{2,}[\s|:-]*$")  # "|---|:--:|", the line under a table head
_NUMBERED = re.compile(r"^\(?(\d{1,2})[.)]\s+(.*)$")
_LETTERED = re.compile(r"^(?:\(([A-Za-z])\)|([A-Za-z])\)|([A-Z])[.:])\s+(.*)$")
_NAMED = re.compile(r"^(?:op[çc][ãa]o|option|alternativa|alternative|escolha|choice)\s+([A-Za-z]|\d{1,2})\b\s*[:.)\-–—]?\s*(.*)$",
                    re.IGNORECASE)
_TOKEN = re.compile(r"[\w.\-/\\:~@'’]+")
_PART = re.compile(r"\w+(?:['’-]\w+)*")
_CAMEL = re.compile(r"^[A-Za-z]+$")
_HUMPS = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")
_SNAKE = re.compile(r"^[A-Za-z]+(?:_[A-Za-z]+)+$")
_HYPHENATED = re.compile(r"^[A-Za-z]+(?:-[A-Za-z]+)+$")
_FILE = re.compile(r"^\.?[A-Za-z_][\w-]*(?:\.[\w-]+)*\.([A-Za-z]{1,8})$")
_DOTFILE = re.compile(r"^\.[A-Za-z][\w-]+$")
_EDGES = "._-:~@'’"
SENTENCE_END = ".!?:"


@dataclass(frozen=True)
class ReplyOption:
    """One offered choice: its label ("1", "a", "B") and up to ``MAX_HEAD_WORDS`` head words."""

    label: str
    head: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ReplyContext:
    """What a reply offers the user's answer: terms, options, whether it asks, and its word count."""

    terms: tuple[str, ...] = ()
    options: tuple[ReplyOption, ...] = ()
    asks: bool = False
    words: int = 0

    def __repr__(self) -> str:
        return (f"ReplyContext(terms={len(self.terms)}, options={len(self.options)}, asks={self.asks}, "
                f"words={self.words})")

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self.terms or self.options or self.asks)


EMPTY = ReplyContext()


# ---------------------------------------------------------------- tokens


def _has_digit(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def _file_name(token: str) -> str | None:
    """``token`` when it is a file name with a known extension (or a dotfile), else None."""
    match = _FILE.match(token)
    if match and match.group(1).casefold() in FILE_EXTENSIONS:
        return token
    return token if _DOTFILE.match(token) else None


def _base_name(target: str) -> str | None:
    """The file base name at the end of a URL or path, or None."""
    target = target.strip("<>").split("?", 1)[0].split("#", 1)[0]
    last = re.split(r"[/\\]", target.rstrip("/\\"))[-1]
    if "://" in target and target.count("/") < 3:
        return None  # a bare host: "https://example.com"
    return _file_name(last)


def _identifier(token: str) -> str | None:
    """``token`` when it is shaped like a CamelCase, snake_case or hyphenated name, else None."""
    if _SNAKE.match(token) or _HYPHENATED.match(token):
        return token
    if _CAMEL.match(token) and _HUMPS.search(token):
        return token
    return None


def _usable(term: str) -> bool:
    letters = sum(ch.isalpha() for ch in term)
    return (MIN_TERM_CHARS <= letters and len(term) <= MAX_TERM_CHARS and not _has_digit(term)
            and unicodedata.normalize("NFC", term).casefold() not in DROPPED and bool(sound_key(term)))


def _code_terms(token: str) -> list[str]:
    """Identifiers of one code token: a file base name, a shaped name, or the shaped parts of a dotted name."""
    token = token.strip(_EDGES)
    if not token:
        return []
    if "/" in token or "\\" in token or "://" in token:
        name = _base_name(token)
        return [name] if name else []
    name = _file_name(token) or _identifier(token)
    if name:
        return [name]
    if "." in token:
        return [part for piece in token.split(".") for part in [_identifier(piece.strip(_EDGES))] if part]
    return []


def _prose_terms(token: str, sentence_start: bool) -> list[tuple[int, str]]:
    """(kind, term) of one prose token: file names and shaped names are identifiers, other words content."""
    token = token.strip(_EDGES)
    if not token:
        return []
    if "/" in token or "\\" in token or "://" in token:
        name = _base_name(token)
        return [(IDENTIFIER, name)] if name else []
    name = _file_name(token)
    if name:
        return [(IDENTIFIER, name)]
    if _SNAKE.match(token) or (_CAMEL.match(token) and _HUMPS.search(token)):
        return [(IDENTIFIER, token)]
    found = []
    for word in _PART.findall(token):
        if "_" in word:
            continue
        if sentence_start and word[:1].isupper() and word[1:].islower():
            word = word.lower()  # a capital only because the sentence starts here
        found.append((CONTENT, word))
        sentence_start = False
    return found


# ---------------------------------------------------------------- lines


def _strip_inline(line: str, code: list[str]) -> str:
    """Prose of one line: inline code, link targets and URLs moved to ``code``, markup removed."""
    def span(match: re.Match) -> str:
        code.append(match.group(2))
        return " "

    def link(match: re.Match) -> str:
        code.append(match.group(2))
        return f" {match.group(1)} "

    def url(match: re.Match) -> str:
        code.append(match.group(0))
        return " "

    line = _CODE_SPAN.sub(span, line)
    line = _IMAGE.sub(" ", line)
    line = _LINK.sub(link, line)
    line = _URL.sub(url, line)
    line = _TAG.sub(" ", line)
    return _EMPHASIS.sub("", line)


def _option(line: str) -> tuple[str, str, str] | None:
    """(style, label, text) when a prose line starts with a numbered or labelled choice."""
    plain = "".join(_EMPHASIS.sub("", prose) if code is None else f"`{code}`" for prose, code in _pieces(line))
    line = _BULLET.sub("", _LINE_MARK.sub("", plain)).strip()
    match = _NAMED.match(line)
    if match:
        return "named", match.group(1).upper(), match.group(2)
    match = _NUMBERED.match(line)
    if match:
        return "numbered", str(int(match.group(1))), match.group(2)
    match = _LETTERED.match(line)
    if match:
        label = match.group(1) or match.group(2) or match.group(3)
        return "lettered", label, match.group(4)
    return None


def _first(label: str) -> bool:
    return label in ("1", "a", "A")


@dataclass
class _Reading:
    """Terms found so far, with their kind, paragraph and position."""

    found: list[tuple[int, int, int, str]] = field(default_factory=list)
    paragraph: int = 0
    prose_paragraph: int = -1  # the last paragraph that held prose
    question: bool = False  # the last prose paragraph holds a question mark
    words: int = 0
    lists: list[list[tuple[str, tuple[str, ...]]]] = field(default_factory=list)
    style: str | None = None
    previous: str | None = None

    def add(self, kind: int, term: str) -> None:
        self.found.append((kind, self.paragraph, len(self.found), term))

    def code(self, text: str) -> None:
        for token in _TOKEN.findall(text):
            self.words += any(ch.isalnum() for ch in token)
            for term in _code_terms(token):
                self.add(IDENTIFIER, term)

    def prose(self, line: str) -> None:
        spans: list[str] = []
        option = _option(line)
        line = _strip_inline(line, spans)
        line = _BULLET.sub("", _LINE_MARK.sub("", line))
        for span in spans:
            self.code(span)
        if not line.strip():
            return
        if self.prose_paragraph != self.paragraph:
            self.prose_paragraph, self.question = self.paragraph, False
        self.question = self.question or "?" in line
        for kind, term in _line_terms(line):
            self.add(kind, term)
        self.words += sum(1 for token in _TOKEN.findall(line) if any(ch.isalnum() for ch in token))
        if option is not None:
            self._option(*option)

    def _option(self, style: str, label: str, text: str) -> None:
        head: list[str] = []
        for prose, code in _pieces(text):
            if code is None:
                head.extend(term for _, term in _line_terms(_strip_inline(prose, [])) if _usable(term))
            else:
                head.extend(term for token in _TOKEN.findall(code) for term in _code_terms(token) if _usable(term))
        if style != self.style or _first(label) or label == self.previous:
            self.lists.append([])
        self.style, self.previous = style, label
        self.lists[-1].append((label, tuple(head[:MAX_HEAD_WORDS])))

    def blank(self) -> None:
        self.paragraph += 1


def _pieces(text: str) -> list[tuple[str, str | None]]:
    """``text`` split into prose and inline code, in reading order: (prose, None) or ("", code)."""
    pieces: list[tuple[str, str | None]] = []
    at = 0
    for match in _CODE_SPAN.finditer(text):
        pieces.append((text[at:match.start()], None))
        pieces.append(("", match.group(2)))
        at = match.end()
    pieces.append((text[at:], None))
    return pieces


def _line_terms(line: str) -> list[tuple[int, str]]:
    """(kind, term) of a prose line, in reading order; a capital at a sentence start is dropped."""
    found = []
    start = True
    for token in _TOKEN.findall(line):
        found.extend(_prose_terms(token, start))
        start = token.rstrip("\"')]»”")[-1:] in SENTENCE_END
    return found


def _read(text: str) -> _Reading:
    reading = _Reading()
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if fence is not None:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
                reading.blank()
            else:
                reading.code(line)
            continue
        if marker:
            fence = marker.group(1)
            reading.blank()
            continue
        if not line.strip() or _RULE.match(line) or _TABLE_RULE.match(line):
            reading.blank()
            continue
        reading.prose(line.replace("|", " "))
    return reading


# ---------------------------------------------------------------- the context


def derive(text: str | None, max_terms: int = DEFAULT_MAX_TERMS, max_chars: int = DEFAULT_MAX_CHARS) -> ReplyContext:
    """The ``ReplyContext`` of a reply's visible text; empty for no text. Deterministic."""
    if max_terms < 0 or max_chars < 0:
        raise ValueError("max_terms and max_chars must not be negative")
    if not text or not text.strip():
        return EMPTY
    text = unicodedata.normalize("NFC", text[-MAX_INPUT_CHARS:])
    reading = _read(text)
    offered = next((items for items in reversed(reading.lists) if len(items) >= 2), [])[:MAX_OPTIONS]
    options = tuple(ReplyOption(label, head) for label, head in offered)

    ordered = [term for option in options for term in option.head]
    ranked = sorted(reading.found, key=lambda item: (item[0], -item[1], item[2]))
    ordered.extend(term for _, _, _, term in ranked)

    terms: list[str] = []
    keys: set[str] = set()
    used = 0
    for term in ordered:
        if len(terms) >= max_terms:
            break
        key = sound_key(term)
        if key in keys or not _usable(term):
            continue
        cost = len(term) + (len(SEPARATOR) if terms else 0)
        if used + cost > max_chars:
            continue
        keys.add(key)
        terms.append(term)
        used += cost
    asks = bool(options) or reading.question
    return ReplyContext(terms=tuple(terms), options=options, asks=asks, words=reading.words)
