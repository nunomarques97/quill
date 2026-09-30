"""Fail when files Git would publish contain private data.

Scans tracked files and untracked files that are not ignored. Categories:
project-name, personal-name, domain-term, script-text, home-path, api-key,
audio-file. Output shows only ``file:line: category``; the matched text is
never printed.

The script-text rule uses the phrases of the recording scripts, raw and with
placeholders resolved. The committed dictation script (invented text) is the
only file allowed to contain dictation phrases, and the committed prompts
script (``bench/dictation/guiao-prompts-pt.md``, invented text) the only one
allowed to contain the Claude Code prompts; both are still checked against the
commands script and every other rule. The real project names and domain terms
of the prompts set (``[prompts.projects]`` and ``[prompts.terms]`` of
``local/bench.toml`` and each take's manifest) are flagged everywhere. The
phrases of Quill's own voice-command syntax (``PUBLIC_PHRASES``) are public by
design and never script text.

Usage: py -3.12 -m bench.privacy_guard [--config local/bench.toml]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bench.dataset import DatasetError, load_dictation_private_text, load_private_text
from bench.normalize import normalize_words
from bench.prompts import private_text as load_prompts_private_text
from bench.settings import DICTATION_SCRIPT, PROMPTS_SCRIPT, REPO_ROOT, SettingsError, load_settings

NGRAM = 4
# The voice-command syntax Quill itself implements and documents (quill.voice):
# public by design, even where a private recording script says the same words.
PUBLIC_PHRASES = ("abre o VS Code no",)
AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg", ".opus", ".webm", ".m4a", ".aac",
    ".aif", ".aiff", ".wma", ".amr", ".mka",
}
HOME_PATH = re.compile(
    r"(?i)(?<![a-z])[a-z]:[\\/]+users[\\/]+[^\\/\s\"'<>|*?]+"
    r"|(?<![\w.])/(?:home|Users)/[A-Za-z0-9._-]+"
)
API_KEY_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
# KEY_NAME = "value" with a long mixed letter/digit value (for example a
# Deepgram key, which has no fixed prefix).
KEY_ASSIGNMENT = re.compile(
    r"(?i)(?:api|access|auth|secret)[_-]?key\w*[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{20,})"
)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.category}"


def _name_pattern(name: str) -> re.Pattern[str] | None:
    parts = re.findall(r"[^\W_]+", name)
    if not parts:
        return None
    body = r"[\s\-_.]*".join(re.escape(part) for part in parts)
    return re.compile(r"(?<![^\W_])" + body + r"(?![^\W_])", re.IGNORECASE)


@dataclass
class Rules:
    project_names: list[re.Pattern[str]] = field(default_factory=list)
    personal_names: list[re.Pattern[str]] = field(default_factory=list)
    domain_terms: list[re.Pattern[str]] = field(default_factory=list)
    ngrams: set[tuple[str, ...]] = field(default_factory=set)
    # N-grams that only the files in exempt_paths may contain.
    exempt_ngrams: set[tuple[str, ...]] = field(default_factory=set)
    exempt_paths: frozenset[str] = frozenset()
    # Path -> n-grams that only that one file may contain (the prompts script).
    exempt_by_path: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)


def _ngrams(texts: Iterable[str]) -> set[tuple[str, ...]]:
    grams: set[tuple[str, ...]] = set()
    for text in texts:
        words = normalize_words(text)
        for start in range(len(words) - NGRAM + 1):
            grams.add(tuple(words[start : start + NGRAM]))
    return grams


def build_rules(
    project_names: Iterable[str],
    person_name: str | None,
    script_texts: Iterable[str],
    exempt_texts: Iterable[str] = (),
    exempt_paths: Iterable[str] = (),
    *,
    path_texts: Mapping[str, Iterable[str]] | None = None,
    domain_terms: Iterable[str] = (),
) -> Rules:
    """``exempt_texts`` are flagged everywhere except in ``exempt_paths``.

    ``path_texts`` maps one file to texts flagged everywhere except in that
    file; ``domain_terms`` are flagged everywhere, like project names.
    """
    rules = Rules(exempt_paths=frozenset(exempt_paths))
    for name in project_names:
        pattern = _name_pattern(name)
        if pattern is not None:
            rules.project_names.append(pattern)
    for term in domain_terms:
        pattern = _name_pattern(term)
        if pattern is not None:
            rules.domain_terms.append(pattern)
    if person_name and person_name.strip():
        for candidate in [person_name, *re.findall(r"[^\W\d_]{4,}", person_name)]:
            pattern = _name_pattern(candidate)
            if pattern is not None:
                rules.personal_names.append(pattern)
    public = _ngrams(PUBLIC_PHRASES)
    rules.ngrams = _ngrams(script_texts) - public
    rules.exempt_ngrams = _ngrams(exempt_texts) - rules.ngrams - public
    for path, texts in (path_texts or {}).items():
        rules.exempt_by_path[path] = _ngrams(texts) - rules.ngrams - public
    rules.ngrams |= rules.exempt_ngrams
    for grams in rules.exempt_by_path.values():
        rules.ngrams |= grams
    return rules


def _is_key_like(value: str) -> bool:
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def scan_text(path: str, text: str, rules: Rules) -> list[Finding]:
    found: set[Finding] = set()
    exempt = (rules.exempt_ngrams if path in rules.exempt_paths else set()) | rules.exempt_by_path.get(path, set())
    stream: list[tuple[str, int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if any(p.search(line) for p in rules.project_names):
            found.add(Finding(path, number, "project-name"))
        if any(p.search(line) for p in rules.personal_names):
            found.add(Finding(path, number, "personal-name"))
        if any(p.search(line) for p in rules.domain_terms):
            found.add(Finding(path, number, "domain-term"))
        if HOME_PATH.search(line):
            found.add(Finding(path, number, "home-path"))
        if any(p.search(line) for p in API_KEY_PATTERNS) or any(
            _is_key_like(m.group(1)) for m in KEY_ASSIGNMENT.finditer(line)
        ):
            found.add(Finding(path, number, "api-key"))
        if rules.ngrams:
            stream.extend((word, number) for word in normalize_words(line))
    # Script n-grams may wrap across lines, so slide over the whole file.
    for start in range(len(stream) - NGRAM + 1):
        window = tuple(word for word, _ in stream[start : start + NGRAM])
        if window in rules.ngrams and window not in exempt:
            found.add(Finding(path, stream[start][1], "script-text"))
    return sorted(found)


def is_audio(path: str, head: bytes) -> bool:
    if Path(path).suffix.casefold() in AUDIO_EXTENSIONS:
        return True
    return (
        (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
        or head[:3] == b"ID3"
        or head[:4] in (b"fLaC", b"OggS")
        or (head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"))
        or (head[4:8] == b"ftyp" and head[8:11] == b"M4A")
    )


def scan_file(root: Path, rel_path: str, rules: Rules) -> list[Finding]:
    try:
        data = (root / rel_path).read_bytes()
    except OSError:
        return []
    if is_audio(rel_path, data[:16]):
        return [Finding(rel_path, 0, "audio-file")]
    if b"\x00" in data[:8192]:
        return []
    return scan_text(rel_path, data.decode("utf-8", errors="replace"), rules)


def list_files(root: Path) -> list[str]:
    """Tracked files plus untracked files that .gitignore does not exclude."""
    output = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    return sorted({item.decode("utf-8") for item in output.split(b"\x00") if item})


def git_user_name(root: Path) -> str | None:
    result = subprocess.run(["git", "config", "user.name"], cwd=root, capture_output=True)
    name = result.stdout.decode("utf-8", errors="replace").strip()
    return name or None


def scan(root: Path, files: Iterable[str], rules: Rules) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path in files:
        findings.extend(scan_file(root, rel_path, rules))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.privacy_guard", description="Scan publishable files for private data.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
        texts, names = load_private_text(settings)
        dictation_texts, dictation_names = load_dictation_private_text(settings.dictation, settings)
        prompt_texts, prompt_names, prompt_terms = load_prompts_private_text(settings.prompts)
    except (SettingsError, DatasetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    person = git_user_name(REPO_ROOT)
    if person is None:
        print("warning: git user.name is not set; personal-name check skipped", file=sys.stderr)
    exempt_path = DICTATION_SCRIPT.relative_to(REPO_ROOT).as_posix()
    prompts_path = PROMPTS_SCRIPT.relative_to(REPO_ROOT).as_posix()
    rules = build_rules(names | dictation_names | prompt_names, person, texts, dictation_texts, [exempt_path],
                        path_texts={prompts_path: prompt_texts}, domain_terms=prompt_terms)
    try:
        files = list_files(REPO_ROOT)
    except (OSError, subprocess.CalledProcessError):
        print("error: cannot list repository files with git", file=sys.stderr)
        return 2

    findings = scan(REPO_ROOT, files, rules)
    for finding in findings:
        print(finding)
    if findings:
        print(f"privacy guard: FAIL, {len(findings)} finding(s) in {len({f.path for f in findings})} file(s)")
        return 1
    print(f"privacy guard: OK, {len(files)} file(s) scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
