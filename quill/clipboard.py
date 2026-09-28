"""Clipboard snapshots: save every format, compare, and restore.

The injector types with Unicode keyboard events and never writes to the
clipboard. This module proves it: the typing self-test compares a snapshot
taken before and after each target, and restores the saved contents if
anything changed them. Comparisons and messages name format ids only, never
the contents.

Formats held as GDI handles (bitmaps, palettes, metafiles, owner display) and
private formats (whose handle type only the owning program knows) are recorded
by id without data: they cannot be copied as bytes. The DIB formats Windows
offers next to a bitmap are regular memory blocks and are saved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_PALETTE = 9
CF_ENHMETAFILE = 14
CF_OWNERDISPLAY = 0x0080
CF_DSPBITMAP = 0x0082
CF_DSPMETAFILEPICT = 0x0083
CF_DSPENHMETAFILE = 0x008E
CF_PRIVATEFIRST = 0x0200
CF_PRIVATELAST = 0x02FF
CF_GDIOBJFIRST = 0x0300
CF_GDIOBJLAST = 0x03FF
CF_REGISTERED_FIRST = 0xC000

HANDLE_FORMATS = frozenset({
    CF_BITMAP, CF_METAFILEPICT, CF_PALETTE, CF_ENHMETAFILE,
    CF_OWNERDISPLAY, CF_DSPBITMAP, CF_DSPMETAFILEPICT, CF_DSPENHMETAFILE,
})


class ClipboardError(OSError):
    """The clipboard could not be opened, read or restored."""


def is_handle_format(fmt: int) -> bool:
    return fmt in HANDLE_FORMATS or CF_PRIVATEFIRST <= fmt <= CF_GDIOBJLAST


@dataclass(frozen=True)
class ClipboardFormat:
    fmt: int
    name: str = ""
    data: bytes | None = None


@dataclass(frozen=True)
class ClipboardSnapshot:
    formats: tuple[ClipboardFormat, ...]
    sequence: int

    @property
    def format_ids(self) -> tuple[int, ...]:
        return tuple(item.fmt for item in self.formats)

    def same_content(self, other: ClipboardSnapshot) -> bool:
        """Same formats, in the same order, with the same bytes."""
        return self.formats == other.formats

    def identical(self, other: ClipboardSnapshot) -> bool:
        """Same content and nobody wrote the clipboard in between."""
        return self.same_content(other) and self.sequence == other.sequence


def describe_difference(before: ClipboardSnapshot, after: ClipboardSnapshot) -> str:
    """Which format ids differ; never the contents."""
    parts: list[str] = []
    if before.sequence != after.sequence:
        parts.append("sequence number changed")
    old = {item.fmt: item for item in before.formats}
    new = {item.fmt: item for item in after.formats}
    removed = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    changed = sorted(fmt for fmt in set(old) & set(new) if old[fmt] != new[fmt])
    if removed:
        parts.append("formats removed " + ", ".join(map(str, removed)))
    if added:
        parts.append("formats added " + ", ".join(map(str, added)))
    if changed:
        parts.append("format data changed " + ", ".join(map(str, changed)))
    if not (removed or added or changed) and before.format_ids != after.format_ids:
        parts.append("format order changed")
    return "; ".join(parts) or "identical"


@contextmanager
def opened(
    api: object, *, attempts: int = 20, delay_s: float = 0.025, sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Open the clipboard, retrying while another program holds it."""
    for attempt in range(attempts):
        if api.open_clipboard():
            break
        if attempt + 1 == attempts:
            raise ClipboardError("clipboard is held by another program")
        sleep(delay_s)
    try:
        yield
    finally:
        api.close_clipboard()


def snapshot(api: object, **open_options: object) -> ClipboardSnapshot:
    """Every format on the clipboard with its bytes (handle formats by id only)."""
    with opened(api, **open_options):
        # Read while the clipboard is open: nobody can write it until it is closed.
        sequence = api.clipboard_sequence()
        items = []
        for fmt in api.clipboard_formats():
            name = api.clipboard_format_name(fmt) if fmt >= CF_REGISTERED_FIRST else ""
            data = None if is_handle_format(fmt) else api.clipboard_bytes(fmt)
            items.append(ClipboardFormat(fmt, name, data))
    return ClipboardSnapshot(tuple(items), sequence)


def restore(api: object, saved: ClipboardSnapshot, **open_options: object) -> list[int]:
    """Put a snapshot back on the clipboard; returns the format ids that could not be restored."""
    skipped: list[int] = []
    with opened(api, **open_options):
        if not api.empty_clipboard():
            raise ClipboardError("clipboard could not be emptied")
        for item in saved.formats:
            if item.data is None or not api.set_clipboard_bytes(item.fmt, item.data):
                skipped.append(item.fmt)
    return skipped
