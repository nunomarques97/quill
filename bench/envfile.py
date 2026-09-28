"""API keys from the ignored .env file, and redaction of their values.

Keys are read from .env only, never from the process environment, so a key
set elsewhere on the machine is never picked up by accident. Values are kept
out of reprs; every message that may carry text from a request or response
passes through ``redact`` before it is logged, raised or stored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bench.settings import REPO_ROOT

ENV_FILE = REPO_ROOT / ".env"
KEY_NAMES = ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPGRAM_API_KEY")
REDACTED = "[redacted]"
# Shorter values are too generic to redact safely (they would mangle text);
# real provider keys are far longer.
MIN_REDACT_LENGTH = 8


class EnvFileError(Exception):
    """The .env file exists but a line cannot be parsed. Never carries values."""


@dataclass(frozen=True)
class Keys:
    """Key values by name. The repr lists names only."""

    values: Mapping[str, str] = field(default_factory=dict, repr=False)

    def get(self, name: str) -> str | None:
        value = self.values.get(name)
        return value if value else None

    def present(self) -> list[str]:
        return [name for name in KEY_NAMES if self.get(name)]

    def missing(self) -> list[str]:
        return [name for name in KEY_NAMES if not self.get(name)]

    def secret_values(self) -> list[str]:
        return [value for value in self.values.values() if value]

    def __repr__(self) -> str:
        return f"Keys(present={self.present()})"


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. '#' starts a comment line; quotes are stripped."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name.replace("_", "").isalnum():
            raise EnvFileError(f".env line {number} is not KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_keys(path: Path | None = None) -> Keys:
    """Keys from .env; a missing file means no keys."""
    env_path = Path(path) if path is not None else ENV_FILE
    if not env_path.is_file():
        return Keys({})
    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise EnvFileError(".env cannot be read as UTF-8 text") from None
    parsed = parse_env(text)
    return Keys({name: parsed[name] for name in KEY_NAMES if parsed.get(name)})


def redact(text: object, secrets: Iterable[str]) -> str:
    """Replace every secret value in ``text``; longest first."""
    result = str(text)
    for value in sorted({s for s in secrets if s and len(s) >= MIN_REDACT_LENGTH}, key=len, reverse=True):
        result = result.replace(value, REDACTED)
    return result
