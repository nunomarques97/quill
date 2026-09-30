"""Quill's on-screen indicator: a holographic glass capsule with the live words.

``Indicator`` (thread-safe) shows the states in ``render.STATES`` with
European Portuguese labels; ``python -m quill.indicator --render-frames DIR``
renders every state offscreen to PNG without creating any window.
"""

from quill.indicator.render import (CLAUDE_DONE, CLAUDE_PERMISSION, COMMAND, ERROR, LABELS, LISTENING, LOADING,
                                    SENT, STATES, TRANSCRIBING, View)
from quill.indicator.window import Indicator, IndicatorError

__all__ = [
    "CLAUDE_DONE", "CLAUDE_PERMISSION", "COMMAND", "ERROR", "LABELS", "LISTENING", "LOADING", "SENT", "STATES",
    "TRANSCRIBING", "View",
    "Indicator", "IndicatorError",
]
