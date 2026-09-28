"""The single text normalizer applied to every engine, variant and reference.

Rules, in order (documented in bench/README.md):

1. Unicode NFC.
2. Casefold. Accents are kept ("é" stays "é").
3. A dash (Unicode category Pd) between two letters becomes a space
   ("lê-me" -> "lê me").
4. Every other punctuation (P*) or symbol (S*) character is deleted
   ("README." -> "readme", "3,5" -> "35").
5. Whitespace is collapsed to single spaces and trimmed.
6. Generic technical spellings are mapped to one canonical token using
   EQUIVALENCES ("vs code" and "vscode" -> "vscode"). Longest match wins.
"""

from __future__ import annotations

import re
import unicodedata

# Canonical token -> spellings that mean the same thing. Only generic
# technical terms belong here; never personal vocabulary or project names.
EQUIVALENCES: dict[str, tuple[str, ...]] = {
    "vscode": ("vs code", "visual studio code"),
    "readme": ("read me",),
    "github": ("git hub",),
    "javascript": ("java script",),
    "typescript": ("type script",),
    "localhost": ("local host",),
    "email": ("e mail",),
    "online": ("on line",),
    "wifi": ("wi fi",),
    "backend": ("back end",),
    "frontend": ("front end",),
    "chatgpt": ("chat gpt",),
}

_WHITESPACE = re.compile(r"\s+")


def _is_letter(char: str) -> bool:
    return unicodedata.category(char).startswith("L")


def _strip_punctuation(text: str) -> str:
    out: list[str] = []
    last = len(text) - 1
    for index, char in enumerate(text):
        category = unicodedata.category(char)
        if category == "Pd":
            before = text[index - 1] if index > 0 else ""
            after = text[index + 1] if index < last else ""
            if before and after and _is_letter(before) and _is_letter(after):
                out.append(" ")
            continue
        if category[0] in ("P", "S"):
            continue
        out.append(char)
    return "".join(out)


def _build_equivalence_table() -> list[tuple[tuple[str, ...], str]]:
    table: list[tuple[tuple[str, ...], str]] = []
    for canonical, variants in EQUIVALENCES.items():
        for variant in variants:
            table.append((tuple(variant.split()), canonical))
    # Longest variants first so "visual studio code" wins over shorter ones.
    table.sort(key=lambda item: len(item[0]), reverse=True)
    return table


_EQUIVALENCE_TABLE = _build_equivalence_table()


def apply_equivalences(words: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(words):
        for variant, canonical in _EQUIVALENCE_TABLE:
            end = index + len(variant)
            if tuple(words[index:end]) == variant:
                out.append(canonical)
                index = end
                break
        else:
            out.append(words[index])
            index += 1
    return out


def normalize_words(text: str) -> list[str]:
    """Return the normalized word list of ``text``."""
    text = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    text = _strip_punctuation(text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return []
    return apply_equivalences(text.split(" "))


def normalize(text: str) -> str:
    """Return the normalized text as one space-separated string."""
    return " ".join(normalize_words(text))
