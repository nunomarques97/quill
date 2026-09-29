"""Product tests. They never create windows, install hooks, send input, open the
microphone or touch the real clipboard: importing this package disables the
real Win32 layer, the real hook installer and the real indicator window layer,
so any test that reaches them fails instead of acting.
"""

from quill import win32


def _forbidden(self: object, *args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use a fake Win32 layer, never the real User32")


win32.User32.__init__ = _forbidden  # type: ignore[method-assign]
win32.LowLevelHooks.__init__ = _forbidden  # type: ignore[method-assign]

from quill.indicator import window as _indicator_window  # noqa: E402

_indicator_window.OverlayWin32.__init__ = _forbidden  # type: ignore[method-assign]
