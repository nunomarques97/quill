"""Machine-specific benchmark settings, read from the ignored local/bench.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "local" / "bench.toml"
RESULTS_DIR = REPO_ROOT / "bench" / "results"
DEFAULT_MANIFEST_NAME = "manifesto.json"
DEFAULT_EXPECTED_TAKES = 44


class SettingsError(Exception):
    """Raised when local/bench.toml is missing or incomplete."""


@dataclass(frozen=True)
class Settings:
    recordings_dir: Path
    recording_script: Path
    reference_config: Path
    manifest: Path
    expected_takes: int = DEFAULT_EXPECTED_TAKES


def load_settings(path: Path | None = None) -> Settings:
    """Load settings; error messages name the missing key, never its value."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG
    if not config_path.is_file():
        raise SettingsError(
            "benchmark settings not found: create local/bench.toml from bench/bench.example.toml"
        )
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"invalid TOML in benchmark settings: {exc}") from None

    paths = data.get("paths")
    if not isinstance(paths, dict):
        raise SettingsError("benchmark settings need a [paths] table")

    def required(key: str) -> Path:
        value = paths.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"benchmark settings: [paths].{key} is required")
        return Path(value)

    recordings_dir = required("recordings_dir")
    manifest_value = paths.get("manifest")
    if manifest_value is not None and not isinstance(manifest_value, str):
        raise SettingsError("benchmark settings: [paths].manifest must be a string")
    manifest = Path(manifest_value) if manifest_value else recordings_dir / DEFAULT_MANIFEST_NAME

    dataset = data.get("dataset", {})
    expected = dataset.get("expected_takes", DEFAULT_EXPECTED_TAKES) if isinstance(dataset, dict) else None
    if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
        raise SettingsError("benchmark settings: [dataset].expected_takes must be a positive integer")

    return Settings(
        recordings_dir=recordings_dir,
        recording_script=required("recording_script"),
        reference_config=required("reference_config"),
        manifest=manifest,
        expected_takes=expected,
    )
