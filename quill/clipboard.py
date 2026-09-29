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

``copy_selection`` reads the selected text for the correction key (and the
command mode): save every format, copy, read, and restore on every path
without overwriting a change another program made in between.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

log = logging.getLogger("quill.clipboard")

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


# ---------------------------------------------------------------- selection copy

CF_DIB = 8
CF_UNICODETEXT = 13
CF_DIBV5 = 17
VK_CONTROL = 0x11
VK_C = 0x43
KEYEVENTF_KEYUP = 0x0002
MAX_SELECTION_CHARS = 4000

# Outcomes of copy_selection.
COPIED = "copied"
NO_SELECTION = "no_selection"
NO_TEXT = "no_text"
TOO_LONG = "too_long"
BUSY = "busy"
UNSAFE = "unsafe_clipboard"
COPY_FAILED = "copy_failed"


def unrestorable(saved: ClipboardSnapshot) -> list[int]:
    """Format ids a restore would lose (Windows rebuilds a bitmap from a saved DIB)."""
    kept = {item.fmt for item in saved.formats if item.data is not None}
    return [item.fmt for item in saved.formats
            if item.data is None and not (item.fmt == CF_BITMAP and kept & {CF_DIB, CF_DIBV5})]


@dataclass(frozen=True)
class CopyResult:
    """The copied selection (never logged) and what happened to the user's clipboard."""

    text: str | None = field(repr=False)
    reason: str
    restored: bool

    @property
    def ok(self) -> bool:
        return self.reason == COPIED


def copy_events() -> list[tuple[int, int]]:
    """(virtual key, flags) of Ctrl+C: Ctrl down, C down, C up, Ctrl up."""
    return [(VK_CONTROL, 0), (VK_C, 0), (VK_C, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)]


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    return data.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def copy_selection(
    api: object,
    send_copy: Callable[[], bool],
    *,
    timeout_s: float = 0.5,
    poll_s: float = 0.01,
    max_chars: int = MAX_SELECTION_CHARS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CopyResult:
    """Read the selected text by copying it, then put the user's clipboard back.

    The clipboard is saved first; when it holds something a restore would
    lose, nothing is touched. It is emptied, ``send_copy`` sends Ctrl+C to
    the target and the text is read once the clipboard changes. On every
    path (also on an exception) the saved formats are restored, unless
    another program wrote the clipboard after the copy was read: that
    change is kept, never overwritten. Nothing about the text is logged.
    """
    try:
        saved = snapshot(api, sleep=sleep)
    except ClipboardError:
        log.warning("selection copy: clipboard busy")
        return CopyResult(None, BUSY, True)
    lost = unrestorable(saved)
    if lost:
        log.warning("selection copy refused: clipboard formats %s could not be restored", ", ".join(map(str, lost)))
        return CopyResult(None, UNSAFE, True)
    expected: int | None = None  # the sequence number Quill last saw; None until the clipboard is emptied
    text: str | None = None
    reason = COPY_FAILED
    try:
        with opened(api, sleep=sleep):
            if api.clipboard_sequence() != saved.sequence:
                raise ClipboardError("clipboard changed while it was saved")
            if not api.empty_clipboard():
                raise ClipboardError("clipboard could not be emptied")
            expected = api.clipboard_sequence()
        if send_copy():
            deadline = clock() + timeout_s
            while api.clipboard_sequence() == expected and clock() < deadline:
                sleep(poll_s)
            current = api.clipboard_sequence()
            if current == expected:
                reason = NO_SELECTION
            else:
                # The target's copy: restore over it even when reading it fails below.
                expected = current
                with opened(api, sleep=sleep):
                    formats = api.clipboard_formats()
                    text = _decode(api.clipboard_bytes(CF_UNICODETEXT)) if CF_UNICODETEXT in formats else None
                    expected = api.clipboard_sequence()
                reason = COPIED if text else NO_TEXT
                if text is not None and len(text) > max_chars:
                    text, reason = None, TOO_LONG
    except ClipboardError:
        reason = BUSY
    finally:
        restored = expected is None or _restore_unless_changed(api, saved, expected, sleep)
    return CopyResult(text if reason == COPIED else None, reason, restored)


def _restore_unless_changed(api: object, saved: ClipboardSnapshot, expected: int, sleep: Callable[[float], None]) -> bool:
    try:
        with opened(api, sleep=sleep):
            if api.clipboard_sequence() != expected:
                log.warning("selection copy: clipboard changed by another program; left as it is")
                return False
            if not api.empty_clipboard():
                raise ClipboardError("clipboard could not be emptied")
            # A bitmap without bytes comes back from its saved DIB (see unrestorable).
            skipped = [item.fmt for item in saved.formats
                       if item.data is not None and not api.set_clipboard_bytes(item.fmt, item.data)]
    except ClipboardError:
        log.error("selection copy: clipboard could not be restored")
        return False
    if skipped:
        log.error("selection copy: formats %s could not be restored", ", ".join(map(str, skipped)))
        return False
    return True
