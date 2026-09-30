"""A local, read-only context pack of a project folder: a short summary and its own terms.

``ContextPacks.get(folder)`` gives the ``ContextPack`` of a project folder, or
None (today's behaviour: the dictation goes on without it):

- ``summary``: what the project is, at most ``MAX_SUMMARY_CHARS`` characters
  of the prose paragraphs of its ``CLAUDE.md``, else of its ``README.md``
  (headings, lists such as rule lists, tables, quotes, HTML and code blocks
  are skipped; links keep their text, URLs are dropped);
- ``terms``: at most ``MAX_TERMS`` deduplicated terms (case and accents
  folded) in a deterministic order (score, then spelling): the words of the
  Markdown headings and the identifiers in inline code of README, CLAUDE.md
  and ``docs/`` Markdown, module and file names, class names, and the words
  the project repeats (``MIN_REPEATS`` times or more in its prose, identifiers
  split into their parts). Common Portuguese and English function words are
  never terms.

Which files are read is decided by Git itself: ``git ls-files --cached
--others --exclude-standard`` (tracked, or untracked and not ignored) run
with an argument list (no shell), a timeout, no window, every ``GIT_*``
variable cleared, optional locks off and the file-system monitor off (a
repository's configuration never starts a program here). A folder that is
not in a Git work tree, or no usable ``git``, gives no pack. The listing is
then filtered again, whatever Git says, by a fixed deny list (``denied``):
``.env`` and ``.env.*``, keys and certificates (``*.key``, ``*.pem``,
``*.jks``, ``*.keystore``, ``*.pfx``, ``*.p12``), ``google-services.json``,
``credentials.json`` and other credential files, any name with ``token`` or
``secret``, ``*.local.*`` files, audio and video files, and anything inside
``recordings/``, ``captures/``, ``transcripts/``, ``local/`` or ``.git/``.
Only Markdown and source files are listed at all.

A listed file is opened only when it is a regular file inside the folder
with no link or junction on its way (its real path is its plain path), it
is not a hard link, the file opened is the file checked, and on Windows the
final path of the opened handle is inside the folder. Reads stop at
``MAX_FILE_BYTES`` per file, ``MAX_TOTAL_BYTES`` in total and
``MAX_READ_FILES`` files; a file with NUL bytes or invalid UTF-8 is skipped.

Packs are cached as JSON in the cache folder (inside the ignored ``local/``
folder, ``[project_context] cache``), one file per project folder named by a
hash of its normalised path, written to a temporary file and renamed. A
cached pack is used while the file list Git gives is the same, every file it
read keeps its size and modification time, it is younger than
``[project_context] max_age_h`` and it is well formed; otherwise it is built
again. A lookup (the Git listing, the checks and a build) that takes longer
than ``[project_context] build_timeout_s`` gives no pack and writes nothing.

Nothing here writes to the project, uses the network or runs anything but
``git``. Project content, names and paths are never logged: logs hold reason
codes, counts and timings only.

Usage: py -3.12 -m quill.context_pack --check [--rebuild]
"""

from __future__ import annotations

import argparse
import codecs
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from quill.corrections import FUNCTION_WORDS

log = logging.getLogger("quill.context_pack")

VERSION = 1
MAX_SUMMARY_CHARS = 600
MAX_TERMS = 150
MIN_TERM_CHARS = 3
MIN_WORD_CHARS = 4  # a repeated prose word
MAX_TERM_CHARS = 40
MIN_REPEATS = 3
MAX_LISTED = 5000  # files of Git's listing looked at
MAX_LIST_BYTES = 4 * 1024 * 1024  # Git's listing output
MAX_READ_FILES = 120
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024
MAX_PATH_CHARS = 4096
FUTURE_SLACK_S = 60.0  # a cache stamped later than now by more than this is stale

# Scores of the term sources; repeated prose words add their count.
HEADING_WEIGHT = 3
CODE_WEIGHT = 2
IDENTIFIER_WEIGHT = 2

# Reason codes (logged and printed; never content).
BUILT = "built"
CACHED = "cached"
NO_FOLDER = "no_folder"
NO_GIT = "no_git"
NOT_WORK_TREE = "not_git_work_tree"
TIMEOUT = "timeout"
FAILED = "failed"
STALE = "stale"
AGED = "aged"
CORRUPT = "corrupt"
MISSING = "missing"
CHANGED = "changed"
LISTING = "listing_changed"
WRITE_FAILED = "cache_write_failed"

MARKDOWN = (".md", ".markdown")
CODE = (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts", ".cs",
        ".dart", ".swift", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".sol", ".vue", ".svelte", ".scala")
PATHSPECS = tuple(f"*{suffix}" for suffix in MARKDOWN + CODE)

# The fixed deny list, applied to every part of a listed path whatever Git says.
DENIED_FOLDERS = frozenset({".git", "local", "recordings", "captures", "transcripts"})
DENIED_NAMES = frozenset({"google-services.json", "credentials.json", ".netrc", ".npmrc", ".pypirc", "id_rsa",
                          "id_dsa", "id_ecdsa", "id_ed25519"})
DENIED_WORDS = ("token", "secret")
KEY_SUFFIXES = (".key", ".pem", ".jks", ".keystore", ".pfx", ".p12")
MEDIA_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".opus", ".webm", ".m4a", ".aac", ".wma", ".aif", ".aiff",
                  ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".3gp")

EXTRA_FUNCTION_WORDS = """
não sim mais menos muito muita muitos muitas pouco pouca poucos poucas também já ainda só apenas cada todo toda
todos todas tudo nada algo algum alguma alguns algumas nenhum nenhuma outro outra outros outras mesmo mesma
mesmos mesmas qual quais quem cujo cuja tal tais tanto tanta quanto quanta ser sou és é somos são foi foram era
eram será serão seja sejam sido estar está estão estava estavam esteja ter tenho tem têm tinha tinham terá tido
haver há havia fazer faz fazem feito pode podem poder deve devem vai vão ir aqui ali lá então assim bem sempre
nunca depois antes agora após contra durante perante via etc cá ou seja através também isto
a an are was were be been being am have has had having do does did doing done not no yes can cannot could will
would shall should may might must than then there here when where which who whom whose what why how all any
each every some such only also just more most less other others into onto over under after before about above
below again further once very too own same so both few nor out up down off our ours your yours their theirs his
her hers him them they we you i my me us one ones via etc eg ie per vs let lets get gets got make makes made
""".split()
NOISE_WORDS = "http https www com org net html md readme todo".split()


def fold(text: str) -> str:
    """Case and accents ignored."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return unicodedata.normalize("NFC", "".join(ch for ch in decomposed if not unicodedata.combining(ch)))


STOPWORDS = frozenset(fold(word) for word in (*FUNCTION_WORDS, *EXTRA_FUNCTION_WORDS, *NOISE_WORDS))
# File names that name nothing of the project.
GENERIC_STEMS = frozenset({"readme", "claude", "agents", "changelog", "license", "contributing", "index", "main",
                           "mod", "lib", "init", "setup", "conftest", "__init__", "__main__", "tests", "test"})

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s+(.*?))?\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s")
_SKIPPED = re.compile(r"^\s*(?:\||>|<|\[[^\]]+\]:|!\[)")
_RULE = re.compile(r"^\s{0,3}(?:[-*_=]\s*){3,}$")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_URL = re.compile(r"<?(?:(?:https?|ftp|file)://|www\.)[^\s<>()]*[^\s<>().,;:!?'\"]>?")
_LOOSE_MARK = re.compile(r"\s+([.,;:!?])")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_EMPHASIS = re.compile(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)")
_CODE_SPAN = re.compile(r"`([^`\n]{1,80})`")
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"[^\W\d_][^\W_]*(?:[-'’][^\W_]+)*")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*[A-Za-z0-9_]$")
_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
_CLASS = re.compile(r"^[ \t]*(?:(?:export|default|public|private|protected|internal|abstract|final|sealed|data|"
                    r"static|pub(?:\([a-z]+\))?|open)[ \t]+)*(?:class|interface|struct|enum|trait|record|contract)"
                    r"[ \t]+([A-Za-z_][A-Za-z0-9_]{2,39})", re.MULTILINE)
_LONG_DIGITS = re.compile(r"\d{6,}")


class GitError(Exception):
    """Git could not list the folder; ``reason`` is a reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PackTimeout(Exception):
    """The lookup ran out of its time bound."""


@dataclass(frozen=True)
class ContextPack:
    """A project's summary and terms; ``files`` is how many files were read."""

    summary: str = field(repr=False)
    terms: tuple[str, ...] = field(repr=False)
    files: int = 0
    built_at: float = 0.0


# ---------------------------------------------------------------- the file list


def denied(relative: str) -> bool:
    """True when a relative path (``/`` or ``\\`` separated) may never be read, whatever Git says."""
    if not isinstance(relative, str) or not relative or len(relative) > MAX_PATH_CHARS:
        return True
    if ":" in relative or "\0" in relative or relative[0] in "\\/":
        return True  # a drive, an alternate data stream or an absolute path
    parts = re.split(r"[\\/]", relative)
    for part in parts:
        if part in ("", ".", "..") or part != part.rstrip(". "):
            return True  # Windows would open another name than the one checked
        name = unicodedata.normalize("NFC", part).casefold()
        if (name in DENIED_FOLDERS or name in DENIED_NAMES or name == ".env" or name.startswith(".env.")
                or any(word in name for word in DENIED_WORDS) or ".local." in name or name.endswith(".local")
                or name.endswith(KEY_SUFFIXES) or name.endswith(MEDIA_SUFFIXES)):
            return True
    return False


def readable_kind(relative: str) -> str | None:
    """"markdown" or "code" for a file this module reads, else None."""
    name = relative.rsplit("/", 1)[-1].casefold()
    if name.endswith(MARKDOWN):
        return "markdown"
    if name.endswith(CODE):
        return "code"
    return None


def git_environment() -> dict[str, str]:
    """The environment without any ``GIT_*`` variable (a repository or index given from outside)."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}


class Git:
    """The installed ``git``, run with an argument list and a timeout, never through a shell."""

    def __init__(self, executable: str | None = None) -> None:
        found = executable if executable is not None else shutil.which("git")
        self.executable = found if found and os.path.isabs(found) else None

    def command(self, folder: str) -> list[str]:
        return [self.executable or "git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", folder,
                "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *PATHSPECS]

    def list_files(self, folder: str, timeout_s: float) -> list[str]:
        """Relative paths (``/`` separated) Git tracks or would track, among the Markdown and source files."""
        if self.executable is None:
            raise GitError(NO_GIT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(self.command(folder), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=git_environment(), creationflags=flags,
                                       close_fds=True, shell=False)
        except OSError:
            raise GitError(NO_GIT) from None
        try:
            out, _ = process.communicate(timeout=max(timeout_s, 0.001))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise PackTimeout() from None
        if process.returncode != 0:
            raise GitError(NOT_WORK_TREE)
        if len(out) > MAX_LIST_BYTES:
            out = out[:MAX_LIST_BYTES].rsplit(b"\0", 1)[0]
        names: list[str] = []
        for raw in out.split(b"\0"):
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if name:
                names.append(name)
        return names


def candidates(listed: Iterable[str]) -> list[str]:
    """The listed paths this module may read: allowed kinds, not denied; sorted, unique, capped."""
    kept = sorted({name for name in listed if readable_kind(name) and not denied(name)})
    return kept[:MAX_LISTED]


def listing_hash(names: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(names).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- reading one file safely


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _reparse(status: os.stat_result) -> bool:
    return bool(getattr(status, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(
        status.st_mode)


def _same_path(one: str, other: str) -> bool:
    return os.path.normcase(os.path.normpath(one)) == os.path.normcase(os.path.normpath(other))


def inside(path: str, root: str) -> bool:
    """``path`` is ``root`` or below it (both real, normalised paths)."""
    if path.startswith("\\\\?\\"):
        path = path[4:]
        if path.upper().startswith("UNC\\"):
            return False
    path_key, root_key = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(root))
    return path_key == root_key or path_key.startswith(root_key.rstrip("\\/") + os.sep)


@functools.cache
def _final_path_function() -> Callable[..., int]:
    import ctypes
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    return function


def final_path(fd: int) -> str | None:
    """The final path of an open file (links and junctions resolved) on Windows; None elsewhere."""
    if sys.platform != "win32":
        return None
    import ctypes
    import msvcrt

    buffer = ctypes.create_unicode_buffer(32768)
    length = _final_path_function()(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not 0 < length < len(buffer):
        raise OSError("the final path of the file cannot be read")
    return buffer.value


@dataclass(frozen=True)
class Source:
    """A file read into a pack: its relative path, size and modification time."""

    path: str = field(repr=False)
    size: int
    mtime_ns: int


def read_text(root: str, relative: str, limit: int) -> tuple[str, Source] | None:
    """The UTF-8 text of ``root/relative`` (at most ``limit`` bytes), or None when it may not or cannot be read."""
    if denied(relative) or limit <= 0:
        return None
    path = os.path.join(root, *relative.split("/"))
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or _reparse(before):
            return None
        if not _same_path(os.path.realpath(path), path) or not inside(path, root):
            return None  # a link or a junction on the way
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    except (OSError, ValueError):
        return None
    try:
        status = os.fstat(fd)
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or (status.st_ino, status.st_dev) != (before.st_ino, before.st_dev)):
            return None  # a hard link, or not the file checked above
        final = final_path(fd)
        if final is not None and not inside(final, root):
            return None
        data = b""
        while len(data) < limit:
            chunk = os.read(fd, min(limit - len(data), 65536))
            if not chunk:
                break
            data += chunk
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if b"\0" in data:
        return None
    try:
        # A read cut short drops an incomplete last character; invalid UTF-8 is not text.
        text = codecs.getincrementaldecoder("utf-8")("strict").decode(data, final=len(data) < limit)
    except UnicodeDecodeError:
        return None
    return text.removeprefix("﻿"), Source(relative, status.st_size, status.st_mtime_ns)


# ---------------------------------------------------------------- Markdown


def _plain(text: str) -> str:
    """Inline Markdown to plain text: images and URLs dropped, links keep their text."""
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _REF_LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = text.replace("`", "")
    text = _EMPHASIS.sub("", text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return _LOOSE_MARK.sub(r"\1", _SPACES.sub(" ", text)).strip()


@dataclass
class Markdown:
    """The parts of a Markdown text this module uses."""

    paragraphs: list[tuple[int, str]] = field(default_factory=list)  # (section, text)
    headings: list[str] = field(default_factory=list)
    code_spans: list[str] = field(default_factory=list)
    prose: list[str] = field(default_factory=list)  # every text line outside code blocks


def parse_markdown(text: str) -> Markdown:
    result = Markdown()
    lines = text.splitlines()
    index = 0
    if lines and lines[0].strip() == "---":  # front matter
        for end in range(1, min(len(lines), 200)):
            if lines[end].strip() in ("---", "..."):
                index = end + 1
                break
    fence: str | None = None
    paragraph: list[str] = []
    section = 0

    def close() -> None:
        if paragraph:
            joined = _plain(" ".join(paragraph))
            if joined:
                result.paragraphs.append((section, joined))
            paragraph.clear()

    for line in lines[index:]:
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        if opened:
            close()
            fence = opened.group(1)
            continue
        result.code_spans.extend(_CODE_SPAN.findall(line))
        if not line.strip():
            close()
            continue
        heading = _HEADING.match(line)
        if heading:
            close()
            section += 1
            result.headings.append(_plain(heading.group(1) or ""))
            continue
        if line.startswith(("    ", "\t")) and not paragraph:
            continue  # an indented code block
        result.prose.append(_plain(line))
        if _RULE.match(line):
            if paragraph and line.strip()[0] in "=-":
                result.headings.append(_plain(" ".join(paragraph)))  # a setext heading
            paragraph.clear()
            section += 1
            continue
        if _LIST.match(line) or _SKIPPED.match(line):
            close()
            continue
        paragraph.append(line.strip())
    close()
    return result


def summary_of(markdown: Markdown) -> str:
    """The prose paragraphs of the first section that has any (what the project is), cut at a
    sentence or a word to ``MAX_SUMMARY_CHARS``."""
    text = ""
    first = markdown.paragraphs[0][0] if markdown.paragraphs else 0
    for section, paragraph in markdown.paragraphs:
        if section != first:
            break
        text = f"{text} {paragraph}".strip()
        if len(text) >= MAX_SUMMARY_CHARS:
            break
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    cut = text[:MAX_SUMMARY_CHARS]
    sentence = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence >= MAX_SUMMARY_CHARS // 2:
        return cut[: sentence + 1]
    space = cut.rfind(" ")
    return cut[:space].rstrip(" ,;:-") if space > 0 else cut


# ---------------------------------------------------------------- terms


class Terms:
    """Term candidates with a score and their spellings; the result is deterministic."""

    def __init__(self) -> None:
        self.strong: Counter[str] = Counter()  # headings, inline code, identifiers
        self.repeated: Counter[str] = Counter()  # prose words and identifier parts
        self.spellings: dict[str, Counter[str]] = {}

    @staticmethod
    def _usable(term: str) -> bool:
        return (MIN_TERM_CHARS <= len(term) <= MAX_TERM_CHARS and term.isprintable()
                and not _LONG_DIGITS.search(term) and fold(term) not in STOPWORDS
                and any(ch.isalpha() for ch in term))

    def _note(self, term: str) -> str:
        key = fold(term)
        self.spellings.setdefault(key, Counter())[term] += 1
        return key

    def add(self, term: str, weight: int) -> None:
        term = term.strip()
        if self._usable(term):
            self.strong[self._note(term)] += weight

    def add_words(self, text: str, weight: int) -> None:
        for word in _WORD.findall(text):
            self.add(word, weight)

    def count(self, text: str) -> None:
        for word in _WORD.findall(text):
            if len(word) >= MIN_WORD_CHARS and self._usable(word):
                self.repeated[self._note(word.lower() if word[1:].islower() else word)] += 1

    def identifier(self, name: str, weight: int) -> None:
        """A module, file or class name: a term, and its parts count as repeated words."""
        if not _IDENTIFIER.match(name) or name.casefold() in GENERIC_STEMS:
            return
        self.add(name, weight)
        for part in _PARTS.findall(name):
            self.count(part)

    def result(self) -> tuple[str, ...]:
        keys = set(self.strong) | {key for key, count in self.repeated.items() if count >= MIN_REPEATS}
        scored = sorted(keys, key=lambda key: (-(self.strong[key] + self.repeated[key]), key))
        chosen = []
        for key in scored[:MAX_TERMS]:
            forms = self.spellings[key]
            chosen.append(min(forms, key=lambda form: (-forms[form], form)))
        return tuple(chosen)


def _stem(relative: str) -> str:
    name = relative.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name[1:] else name


def _order(names: Sequence[str]) -> list[str]:
    """Files to read, most telling first: CLAUDE.md, README.md, docs/ Markdown, other READMEs, then code."""
    def rank(name: str) -> tuple[int, int, str]:
        lower = name.casefold()
        depth = lower.count("/")
        if lower == "claude.md":
            group = 0
        elif lower in ("readme.md", "readme.markdown"):
            group = 1
        elif lower.startswith("docs/") and readable_kind(name) == "markdown":
            group = 2
        elif lower.rsplit("/", 1)[-1] in ("readme.md", "readme.markdown"):
            group = 3
        elif readable_kind(name) == "code":
            group = 4
        else:
            group = 9
        return group, depth, lower

    return [name for name in sorted(names, key=rank) if rank(name)[0] < 9]


def build(root: str, names: Sequence[str], deadline: float, clock: Callable[[], float] = time.monotonic
          ) -> tuple[str, tuple[str, ...], list[Source]]:
    """(summary, terms, sources read) from the candidate files of ``root``; raises ``PackTimeout``."""
    terms = Terms()
    sources: list[Source] = []
    summaries: dict[int, str] = {}
    budget = MAX_TOTAL_BYTES
    for relative in _order(names):
        if len(sources) >= MAX_READ_FILES or budget <= 0:
            break
        if clock() > deadline:
            raise PackTimeout()
        limit = min(MAX_FILE_BYTES, budget)
        read = read_text(root, relative, limit)
        if read is None:
            continue
        text, source = read
        sources.append(source)
        budget -= min(source.size, limit)
        if readable_kind(relative) == "markdown":
            markdown = parse_markdown(text)
            lower = relative.casefold()
            if lower in ("claude.md", "readme.md", "readme.markdown"):
                summaries.setdefault(0 if lower == "claude.md" else 1, summary_of(markdown))
            for heading in markdown.headings:
                terms.add_words(heading, HEADING_WEIGHT)
            for span in markdown.code_spans:
                span = span.strip()
                if _IDENTIFIER.match(span) and "/" not in span:
                    terms.add(span, CODE_WEIGHT)
            for line in markdown.prose:
                terms.count(line)
        else:
            for name in _CLASS.findall(text):
                if not name.startswith("_"):
                    terms.identifier(name, IDENTIFIER_WEIGHT)
    for relative in names:
        stem = _stem(relative)
        if readable_kind(relative) == "code" and not stem.casefold().startswith(("test", "_")):
            terms.identifier(stem, IDENTIFIER_WEIGHT)
        elif readable_kind(relative) == "markdown":
            for part in re.split(r"[-_. ]+", stem):
                terms.count(part)
    summary = summaries.get(0) or summaries.get(1) or ""
    return summary, terms.result(), sources


# ---------------------------------------------------------------- the cache


def cache_name(folder: str) -> str:
    """The cache file name of a folder: a hash of its normalised path."""
    normal = os.path.normcase(os.path.normpath(os.path.abspath(folder)))
    return hashlib.sha256(normal.encode("utf-8", "surrogatepass")).hexdigest()[:32] + ".json"


def _unc_or_device(path: str) -> bool:
    return len(path) >= 2 and path[0] in "\\/" and path[1] in "\\/"


class ContextPacks:
    """Context packs of project folders, cached in ``cache_dir`` and checked on every lookup."""

    def __init__(self, cache_dir: Path, *, max_age_s: float = 24 * 3600.0, timeout_s: float = 5.0,
                 git: Git | None = None, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], float] = time.time) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_age_s, self.timeout_s = max_age_s, timeout_s
        self.git = git if git is not None else Git()
        self.clock, self.now = clock, now
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: object, **kwargs: object) -> "ContextPacks":
        """From ``[project_context]`` (``quill.config.ProjectContext``)."""
        return cls(settings.cache_dir, max_age_s=settings.max_age_h * 3600.0, timeout_s=settings.build_timeout_s,
                   **kwargs)

    def get(self, folder: object) -> ContextPack | None:
        """The pack of ``folder``, or None; never raises."""
        return self.lookup(folder)[0]

    def lookup(self, folder: object, *, rebuild: bool = False) -> tuple[ContextPack | None, str]:
        """(pack or None, reason code); ``rebuild`` ignores the cache."""
        started = self.clock()
        try:
            with self._lock:
                pack, reason = self._lookup(folder, started + self.timeout_s, rebuild)
        except PackTimeout:
            pack, reason = None, TIMEOUT
        except Exception as exc:  # noqa: BLE001 - a pack is optional: the dictation goes on without it
            log.warning("context pack: %s (%s)", FAILED, type(exc).__name__)
            return None, FAILED
        elapsed = (self.clock() - started) * 1000
        if pack is None:
            log.info("context pack: none (%s) in %.0f ms", reason, elapsed)
        else:
            log.info("context pack: %s (%d files, %d terms, %d summary characters) in %.0f ms", reason, pack.files,
                     len(pack.terms), len(pack.summary), elapsed)
        return pack, reason

    def _lookup(self, folder: object, deadline: float, rebuild: bool) -> tuple[ContextPack | None, str]:
        if not isinstance(folder, (str, os.PathLike)):
            return None, NO_FOLDER
        text = os.fspath(folder)
        if (not isinstance(text, str) or not text or "\0" in text or len(text) > MAX_PATH_CHARS
                or _unc_or_device(text) or not os.path.isabs(text) or not os.path.isdir(text)):
            return None, NO_FOLDER
        root = os.path.realpath(text)
        if _unc_or_device(root):
            return None, NO_FOLDER  # a link to a network share
        try:
            names = candidates(self.git.list_files(root, deadline - self.clock()))
        except GitError as exc:
            return None, exc.reason
        if self.clock() > deadline:
            raise PackTimeout()
        listing = listing_hash(names)
        target = self.cache_dir / cache_name(text)
        if not rebuild:
            cached, why = self._cached(target, root, listing)
            if cached is not None:
                return cached, CACHED
            log.info("context pack: rebuilding (%s)", why)
        summary, terms, sources = build(root, names, deadline, self.clock)
        if self.clock() > deadline:
            raise PackTimeout()
        pack = ContextPack(summary, terms, len(sources), self.now())
        self._write(target, pack, listing, sources)
        return pack, BUILT

    def _cached(self, target: Path, root: str, listing: str) -> tuple[ContextPack | None, str]:
        try:
            with open(target, "rb") as handle:
                data = handle.read(MAX_CACHE_BYTES + 1)
        except FileNotFoundError:
            return None, MISSING
        except OSError:
            return None, CORRUPT
        try:
            if len(data) > MAX_CACHE_BYTES:
                raise ValueError
            record = json.loads(data.decode("utf-8"))
            pack, sources, cached_listing = _parse_record(record)
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return None, CORRUPT
        age = self.now() - pack.built_at
        if age > self.max_age_s or age < -FUTURE_SLACK_S:
            return None, AGED
        if cached_listing != listing:
            return None, LISTING
        for source in sources:
            try:
                status = os.lstat(os.path.join(root, *source.path.split("/")))
            except (OSError, ValueError):
                return None, CHANGED
            if (status.st_size, status.st_mtime_ns) != (source.size, source.mtime_ns):
                return None, CHANGED
        return pack, CACHED

    def _write(self, target: Path, pack: ContextPack, listing: str, sources: Sequence[Source]) -> None:
        record = {"version": VERSION, "built_at": pack.built_at, "listing": listing, "files": pack.files,
                  "sources": [[source.path, source.size, source.mtime_ns] for source in sources],
                  "summary": pack.summary, "terms": list(pack.terms)}
        temporary: str | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=".pack-", suffix=".tmp", dir=self.cache_dir)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
            os.replace(temporary, target)
            temporary = None
        except OSError as exc:
            log.warning("context pack: %s (%s)", WRITE_FAILED, type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def _parse_record(record: object) -> tuple[ContextPack, list[Source], str]:
    """A cache record checked field by field; ``ValueError`` when anything is off."""
    if not isinstance(record, dict) or record.get("version") != VERSION:
        raise ValueError
    built_at, listing, files = record["built_at"], record["listing"], record["files"]
    summary, terms, sources = record["summary"], record["terms"], record["sources"]
    if (not isinstance(built_at, (int, float)) or isinstance(built_at, bool) or not isinstance(listing, str)
            or not isinstance(files, int) or isinstance(files, bool) or not 0 <= files <= MAX_READ_FILES
            or not isinstance(summary, str) or len(summary) > MAX_SUMMARY_CHARS
            or not isinstance(terms, list) or len(terms) > MAX_TERMS
            or not isinstance(sources, list) or len(sources) != files):
        raise ValueError
    if not all(isinstance(term, str) and 0 < len(term) <= MAX_TERM_CHARS for term in terms):
        raise ValueError
    parsed: list[Source] = []
    for entry in sources:
        if (not isinstance(entry, list) or len(entry) != 3 or not isinstance(entry[0], str) or denied(entry[0])
                or not all(isinstance(value, int) and not isinstance(value, bool) for value in entry[1:])):
            raise ValueError
        parsed.append(Source(entry[0], entry[1], entry[2]))
    return ContextPack(summary, tuple(terms), files, float(built_at)), parsed, listing


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    from quill.config import ConfigError, load_config
    from quill.projects import ProjectFolders

    parser = argparse.ArgumentParser(prog="python -m quill.context_pack", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="build or check the pack of each configured project; prints counts only")
    parser.add_argument("--rebuild", action="store_true", help="build every pack again, ignoring the cache")
    args = parser.parse_args(argv)
    if not args.check:
        parser.print_usage(sys.stderr)
        return 2
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    folders = ProjectFolders(config.project_context.folders, config.voice.shortcut_dirs)
    packs = ContextPacks.from_settings(config.project_context)
    names = folders.names()
    print(f"context pack: {len(names)} configured projects")
    for index, name in enumerate(names, 1):
        folder = folders.folder_for(name)
        if folder is None:
            print(f"project {index}: no folder")
            continue
        started = time.monotonic()
        pack, reason = packs.lookup(folder, rebuild=args.rebuild)
        elapsed = (time.monotonic() - started) * 1000
        if pack is None:
            print(f"project {index}: no pack ({reason}) in {elapsed:.0f} ms")
        else:
            print(f"project {index}: {reason}, {pack.files} files read, {len(pack.summary)} summary characters, "
                  f"{len(pack.terms)} terms, in {elapsed:.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
