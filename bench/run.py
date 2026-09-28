"""Benchmark entry point.

Usage: py -3.12 -m bench.run --dry-run [--config local/bench.toml]

--dry-run loads the real dataset in place and prints counts only; it never
prints spoken text, project names or paths. It exits 0 only when exactly the
expected number of valid takes (44) is available.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from bench.dataset import DatasetError, load_dataset
from bench.settings import SettingsError, load_settings


def dry_run(config: Path | None) -> int:
    try:
        settings = load_settings(config)
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        dataset = load_dataset(settings)
    except DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("placeholders resolved: no")
        return 1

    valid = len(dataset.takes)
    print(f"valid takes: {valid} (expected {settings.expected_takes})")
    print(f"discarded takes excluded (*.invalida-*): {dataset.discarded}")
    print(f"invalid takes excluded: {len(dataset.invalid)}")
    for reason, count in sorted(Counter(item.reason for item in dataset.invalid).items()):
        print(f"  {reason}: {count}")
    print(f"placeholders resolved: {'yes' if dataset.placeholders_resolved else 'no'}")
    origins = ", ".join(f"{origin}={count}" for origin, count in sorted(dataset.origins.items()))
    print(f"recording origin: {origins or 'none'}")
    if valid != settings.expected_takes:
        print(f"FAIL: {valid} valid takes, {settings.expected_takes} required")
        return 1
    print("OK: dataset ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.run", description="Quill engine benchmark.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--dry-run", action="store_true", help="load the dataset and print counts only")
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run(args.config)
    parser.error("no engines are implemented yet; use --dry-run")
    return 2


if __name__ == "__main__":
    sys.exit(main())
