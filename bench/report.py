"""Render and check the marked Markdown comparison block from a summary JSON.

Usage:
    py -3.12 -m bench.report [--summary PATH] [--lang pt|en]   # print the block
    py -3.12 -m bench.report --write DOC.md                     # insert/replace it
    py -3.12 -m bench.report --check DOC.md                     # exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bench.settings import DEFAULT_EXPECTED_TAKES, REPO_ROOT

DEFAULT_SUMMARY = REPO_ROOT / "docs" / "research" / "engines-summary.json"
END_MARKER = "<!-- bench:summary:end -->"
BLOCK = re.compile(
    r"<!-- bench:summary:start(?: lang=(?P<lang>[a-z]+))? -->\n(?P<body>.*?)" + re.escape(END_MARKER),
    re.DOTALL,
)

HEADERS = {
    "pt": ("Motor", "Variante", "n", "WER", "Erro termos EN", "Erro nomes", "Intenção preservada", "p50 (s)", "p95 (s)", "Estado"),
    "en": ("Engine", "Variant", "n", "WER", "English-term error", "Name error", "Intent preserved", "p50 (s)", "p95 (s)", "Status"),
}
EMPTY = "—"


def _decimal(value: float, digits: int, lang: str) -> str:
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if lang == "pt" else text


def _percent(value: float | None, lang: str) -> str:
    if value is None:
        return EMPTY
    return _decimal(100 * value, 1, lang) + ("\u00a0%" if lang == "pt" else "%")


def _seconds(value: float | None, lang: str) -> str:
    return EMPTY if value is None else _decimal(value, 2, lang)


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_block(summary: dict, lang: str = "pt") -> str:
    if lang not in HEADERS:
        raise ValueError(f"unsupported language: {lang}")
    lines = [f"<!-- bench:summary:start lang={lang} -->"]
    headers = HEADERS[lang]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for result in summary.get("results", []):
        if result.get("status") == "ok":
            status = "ok"
        else:
            status = "SKIPPED: " + str(result.get("skipped_reason") or "")
        row = (
            result.get("engine", ""),
            result.get("variant", ""),
            result.get("n", 0),
            _percent(result.get("wer"), lang),
            _percent(result.get("term_error_rate"), lang),
            _percent(result.get("name_error_rate"), lang),
            _percent(result.get("intent_preserved"), lang),
            _seconds(result.get("latency_p50_s"), lang),
            _seconds(result.get("latency_p95_s"), lang),
            status,
        )
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def find_block(document: str) -> re.Match[str] | None:
    return BLOCK.search(document.replace("\r\n", "\n"))


def write_block(document: str, summary: dict, lang: str = "pt") -> str:
    document = document.replace("\r\n", "\n")
    block = render_block(summary, lang).rstrip("\n")
    match = BLOCK.search(document)
    if match is None:
        separator = "" if document.endswith("\n\n") or not document else ("\n" if document.endswith("\n") else "\n\n")
        return document + separator + block + "\n"
    return document[: match.start()] + block + document[match.end() :]


def check(document: str, summary: dict, expected_n: int = DEFAULT_EXPECTED_TAKES) -> list[str]:
    """Return the problems found; an empty list means the document is in sync."""
    problems: list[str] = []
    results = summary.get("results")
    if not isinstance(results, list) or not results:
        problems.append("summary has no results")
        results = []
    for result in results:
        label = f"{result.get('engine')}/{result.get('variant')}"
        status = result.get("status")
        if status == "skipped":
            if not str(result.get("skipped_reason") or "").strip():
                problems.append(f"{label}: SKIPPED without a reason")
        elif status == "ok":
            if result.get("n") != expected_n:
                problems.append(f"{label}: n={result.get('n')} but {expected_n} expected and no SKIPPED reason")
        else:
            problems.append(f"{label}: unknown status")
    match = find_block(document)
    if match is None:
        problems.append("comparison block not found in the document")
    else:
        lang = match.group("lang") or "pt"
        if lang not in HEADERS:
            problems.append(f"comparison block has an unsupported language: {lang}")
        elif match.group(0) + "\n" != render_block(summary, lang):
            problems.append("comparison block differs from the summary; regenerate it with --write")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.report", description=__doc__.splitlines()[0])
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--lang", choices=sorted(HEADERS), default="pt")
    parser.add_argument("--expected-n", type=int, default=DEFAULT_EXPECTED_TAKES)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", type=Path, metavar="MD")
    mode.add_argument("--check", type=Path, metavar="MD")
    args = parser.parse_args(argv)

    try:
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read summary {args.summary.name}: {type(exc).__name__}", file=sys.stderr)
        return 2

    if args.check is not None:
        try:
            document = args.check.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {args.check.name}: {type(exc).__name__}", file=sys.stderr)
            return 2
        problems = check(document, summary, args.expected_n)
        for problem in problems:
            print(f"FAIL: {problem}")
        if not problems:
            print("OK: comparison block matches the summary")
        return 1 if problems else 0

    if args.write is not None:
        document = args.write.read_text(encoding="utf-8") if args.write.exists() else ""
        args.write.write_text(write_block(document, summary, args.lang), encoding="utf-8", newline="\n")
        print(f"wrote comparison block to {args.write.name}")
        return 0

    sys.stdout.write(render_block(summary, args.lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
