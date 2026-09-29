"""Weekly review of the learned corrections (prompts in European Portuguese).

Usage:
    py -3.12 -m quill.review            # list, then approve or delete one by one
    py -3.12 -m quill.review --list     # list only
    py -3.12 -m quill.review --check    # print the reminder when a review is due

The corrections come from ``[paths] corrections`` in the configuration
(``local/corrections.json`` by default), or ``--path FILE``. Active
corrections are applied automatically; pending ones (seen in one dictation,
or in conflict with another) are not. Approving makes a correction active
even if it was seen once and wins over its conflicts; deleting removes it
and it is not learned again. The decisions are applied to the file as it is
when the review ends, so corrections the app learned meanwhile are kept.

A reminder is due when ``REVIEW_DAYS`` (7) days passed since the last review
(or since the file was created) and there is at least one correction.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from quill.corrections import ACTIVE, CONFLICT, PENDING, CorrectionStore, Corrections, Entry, parse_iso

log = logging.getLogger("quill.review")

REVIEW_DAYS = 7
DAY_S = 86_400

STATUS_LABELS = {ACTIVE: "ativa", PENDING: "pendente", CONFLICT: "em conflito"}
APPROVE, DELETE, KEEP, STOP = "a", "e", "", "t"
PROMPT = "Aprovar (a), eliminar (e), manter (Enter) ou terminar (t)? "


def days_since_review(corrections: Corrections, now: float) -> float:
    last = corrections.last_review or corrections.created
    return (now - parse_iso(last)) / DAY_S


def review_due(corrections: Corrections, now: float, days: int = REVIEW_DAYS) -> bool:
    return bool(corrections.entries) and days_since_review(corrections, now) >= days


def reminder(corrections: Corrections, now: float, days: int = REVIEW_DAYS) -> str | None:
    """The reminder text when a review is due, else None. Logs the event without any correction."""
    if not review_due(corrections, now, days):
        return None
    counts = corrections.counts()
    log.info("corrections review due (%d days)", int(days_since_review(corrections, now)))
    return (f"Passaram {int(days_since_review(corrections, now))} dias desde a última revisão das correções "
            f"aprendidas ({counts['active']} ativas, {counts['pending'] + counts['conflict']} pendentes). "
            "Para as rever: py -3.12 -m quill.review")


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def describe(entry: Entry, status: str) -> str:
    label = "aprovada" if entry.approved and status == ACTIVE else STATUS_LABELS[status]
    return f"«{entry.source}» → «{entry.target}» ({label}; vista em {_plural(entry.seen, 'ditado', 'ditados')})"


def ordered(corrections: Corrections) -> list[tuple[Entry, str]]:
    """Active first, then pending and conflicting, each in learning order."""
    statuses = corrections.statuses()
    items = [(entry, statuses[index]) for index, entry in enumerate(corrections.entries)]
    return [item for item in items if item[1] == ACTIVE] + [item for item in items if item[1] != ACTIVE]


def listing(corrections: Corrections) -> list[str]:
    items = ordered(corrections)
    if not items:
        return ["Não há correções aprendidas."]
    lines: list[str] = []
    active = [item for item in items if item[1] == ACTIVE]
    pending = [item for item in items if item[1] != ACTIVE]
    number = 0
    for title, group in (("Correções ativas (aplicadas automaticamente):", active),
                         ("Correções pendentes (ainda não aplicadas):", pending)):
        if not group:
            continue
        lines.append(title)
        for entry, status in group:
            number += 1
            lines.append(f"  {number}. {describe(entry, status)}")
    return lines


def _decide(ask: Callable[[str], str], out: Callable[[str], None], question: str) -> str:
    while True:
        try:
            answer = ask(question).strip().lower()
        except EOFError:
            return STOP
        if answer in (APPROVE, DELETE, KEEP, STOP):
            return answer
        out("Resposta inválida: escreva a, e ou t, ou carregue em Enter.")


def run_review(store: CorrectionStore, ask: Callable[[str], str], out: Callable[[str], None],
               clock: Callable[[], float] = time.time) -> int:
    corrections = store.load()
    for line in listing(corrections):
        out(line)
    decisions: dict[tuple, str] = {}
    items = ordered(corrections)
    for number, (entry, status) in enumerate(items, start=1):
        answer = _decide(ask, out, f"{number}/{len(items)} {describe(entry, status)}\n  {PROMPT}")
        if answer == STOP:
            break
        decisions[(entry.key, entry.target_key)] = answer
    # Apply to the file as it is now: the app may have learned while the review was open.
    current = store.load() if store.changed() else corrections
    approved = deleted = 0
    for entry in list(current.entries):
        answer = decisions.get((entry.key, entry.target_key))
        if answer == APPROVE:
            current.approve(entry)
            approved += 1
        elif answer == DELETE:
            current.delete(entry)
            deleted += 1
    current.mark_reviewed(clock())
    if not store.save(current):
        out("Não foi possível guardar a revisão (ver o registo).")
        return 1
    kept = len(items) - approved - deleted
    out(f"Revisão guardada: {_plural(approved, 'aprovada', 'aprovadas')}, "
        f"{_plural(deleted, 'eliminada', 'eliminadas')}, {_plural(kept, 'mantida', 'mantidas')}.")
    log.info("corrections reviewed: %d approved, %d deleted, %d kept", approved, deleted, kept)
    return 0


def default_path() -> Path:
    from quill.config import load_config

    return load_config().corrections_path


def main(argv: list[str] | None = None, *, ask: Callable[[str], str] = input, out: Callable[[str], None] = print,
         clock: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.review", description="Revisão das correções aprendidas.")
    parser.add_argument("--path", type=Path, default=None, help="ficheiro das correções (por omissão o da configuração)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="só mostrar as correções")
    mode.add_argument("--check", action="store_true", help="mostrar o lembrete quando a revisão estiver em atraso")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.path is None:
        from quill.config import ConfigError

        try:
            args.path = default_path()
        except ConfigError as exc:
            out(f"Configuração inválida: {exc}")
            return 2
    store = CorrectionStore(args.path, clock)
    if args.check:
        message = reminder(store.load(), clock())
        out(message or "A revisão das correções está em dia.")
        return 0
    if args.list:
        for line in listing(store.load()):
            out(line)
        return 0
    return run_review(store, ask, out, clock)


if __name__ == "__main__":
    sys.exit(main())
