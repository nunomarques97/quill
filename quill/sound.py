"""Short alert sounds through the standard library's ``winsound`` (nothing installed).

``WinsoundPlayer`` plays a Windows system sound by alias, asynchronously, so
the caller never waits for it; ``stop`` ends a sound still playing (a
dictation about to open the microphone calls it). Each alert kind has its own
sound: ``done`` (Claude Code finished its reply) and ``permission`` (Claude
Code asks for a permission). The sounds follow the Windows sound scheme; a
scheme with no sound for an alias plays nothing.

Tests never create a ``WinsoundPlayer``: importing ``quill.tests`` makes its
constructor fail. The only manual way to hear the sounds is:

    py -3.12 -m quill.sound --play-sound
"""

from __future__ import annotations

import argparse
import sys
import time

DONE = "done"
PERMISSION = "permission"
KINDS = (DONE, PERMISSION)
# Windows system sound aliases, one per alert kind.
ALIASES = {DONE: "SystemAsterisk", PERMISSION: "SystemExclamation"}
DEMO_GAP_S = 1.5

REFUSAL = (
    "refusing to run: this plays the alert sounds on the speakers.\n"
    "Pass --play-sound to hear them."
)


class WinsoundPlayer:
    """The real player: ``winsound.PlaySound`` with a system alias, never waiting for it."""

    def __init__(self) -> None:
        import winsound

        self._winsound = winsound

    def play(self, kind: str) -> None:
        ws = self._winsound
        ws.PlaySound(ALIASES.get(kind, ALIASES[DONE]), ws.SND_ALIAS | ws.SND_ASYNC | ws.SND_NODEFAULT)

    def stop(self) -> None:
        """End a sound this process is playing (no effect when none is)."""
        self._winsound.PlaySound(None, 0)


def main(argv: list[str] | None = None, player: object | None = None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.sound", description="Play Quill's alert sounds (manual).")
    parser.add_argument("--play-sound", action="store_true", help="required: play each alert sound once")
    args = parser.parse_args(argv)
    if not args.play_sound:
        print(REFUSAL, file=sys.stderr)
        return 2
    player = WinsoundPlayer() if player is None else player
    for kind in KINDS:
        print(f"playing the '{kind}' sound ({ALIASES[kind]})")
        player.play(kind)
        sleep(DEMO_GAP_S)
    player.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
