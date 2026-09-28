"""Quill: push-to-talk dictation for Windows 11 (Python 3.12, standard library only)."""

import logging

# The app configures its own log file; library use stays silent by default.
logging.getLogger("quill").addHandler(logging.NullHandler())
