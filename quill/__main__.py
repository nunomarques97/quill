"""Entry point: ``python -m quill`` (see ``quill.app``).

The start-with-Windows entry runs this file as a script with pythonw
(``quill.startup``); the repository folder is then put on the import path so
the ``quill`` package is found wherever Windows starts it from.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quill.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
