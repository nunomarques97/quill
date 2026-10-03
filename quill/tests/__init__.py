"""Product tests. They never create windows, install hooks, send input, open the
microphone, play sound, open shortcuts or programs, read other processes or touch
the real clipboard or Claude Code settings: importing this package disables the real Win32 layer,
the real hook installer, the real process reader, the real UI Automation reader, the real indicator window layer, the real sound
player, the real speaker of the project names (Windows PowerShell), the real named events of the Claude Code alerts, the real launcher of
the voice commands and the default paths of the user's Claude Code settings and
Claude Code config (``.claude.json``, read by ``quill.names``), so any test that reaches them fails instead of acting.
"""

from quill import win32


def _forbidden(self: object, *args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use a fake Win32 layer, never the real User32")


win32.User32.__init__ = _forbidden  # type: ignore[method-assign]
win32.LowLevelHooks.__init__ = _forbidden  # type: ignore[method-assign]
win32.Processes.__init__ = _forbidden  # type: ignore[method-assign]

from quill import uia as _uia  # noqa: E402

_uia.ComUia.__init__ = _forbidden  # type: ignore[method-assign]

from quill.indicator import window as _indicator_window  # noqa: E402

_indicator_window.OverlayWin32.__init__ = _forbidden  # type: ignore[method-assign]

from quill import claude_hooks as _claude_hooks  # noqa: E402
from quill import names as _names  # noqa: E402
from quill import notify as _notify  # noqa: E402
from quill import shortcuts as _shortcuts  # noqa: E402
from quill import sound as _sound  # noqa: E402
from quill import speech as _speech  # noqa: E402


def _no_sound(self: object, *args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use a fake player, never play sound")


def _no_speech(self: object, *args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use a fake speech engine, never start PowerShell or speak")


def _no_events(self: object, *args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use fake alert events, never the real named events")


def _no_launcher(self: object, *args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use a fake launcher, never open a shortcut or start a program")


def _no_user_settings(*args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use a temporary settings file, never the user's Claude Code settings")


def _no_claude_config(*args: object, **kwargs: object) -> None:
    raise AssertionError("quill tests must use a temporary Claude Code config, never the user's .claude.json")


_sound.WinsoundPlayer.__init__ = _no_sound  # type: ignore[method-assign]
_speech.PowerShellSpeech.__init__ = _no_speech  # type: ignore[method-assign]
_notify.Events.__init__ = _no_events  # type: ignore[method-assign]
_shortcuts.ShellLauncher.__init__ = _no_launcher  # type: ignore[method-assign]
# Kept for the tests of the path rule alone (it only computes a path from an environment).
real_user_settings_path = _claude_hooks.user_settings_path
_claude_hooks.user_settings_path = _no_user_settings  # type: ignore[assignment]
real_claude_config_path = _names.claude_config_path
_names.claude_config_path = _no_claude_config  # type: ignore[assignment]
