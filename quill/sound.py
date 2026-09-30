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

With ``--speak`` it plays one example instead: the ``done`` chime, then an
invented project name (or ``--project NAME``) spoken by a Windows voice
(``quill.speech``) at the rate and volume of ``[claude_alert]``:

    py -3.12 -m quill.sound --play-sound --speak [--project NAME]
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
NO_VOICE = "no Windows voice spoke the name (only the chime plays in Quill)"


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


def main(argv: list[str] | None = None, player: object | None = None, sleep=time.sleep,
         engine: object | None = None, settings: object | None = None) -> int:
    """``engine`` speaks (``quill.speech.PowerShellSpeech``) and ``settings`` is the
    ``[claude_alert]`` table (``quill.config.ClaudeAlert``); both default to the real ones."""
    parser = argparse.ArgumentParser(prog="python -m quill.sound", description="Play Quill's alert sounds (manual).")
    parser.add_argument("--play-sound", action="store_true", help="required: play each alert sound once")
    parser.add_argument("--speak", action="store_true",
                        help="play one example: the chime, then a project name spoken by a Windows voice")
    parser.add_argument("--project", metavar="NAME", help="the project name to speak (default: an invented one)")
    args = parser.parse_args(argv)
    if not args.play_sound:
        print(REFUSAL, file=sys.stderr)
        return 2
    if args.project is not None and not args.speak:
        parser.error("--project needs --speak")
    if args.speak:
        return _speak_example(args.project, player, sleep, engine, settings)
    player = WinsoundPlayer() if player is None else player
    for kind in KINDS:
        print(f"playing the '{kind}' sound ({ALIASES[kind]})")
        player.play(kind)
        sleep(DEMO_GAP_S)
    player.stop()
    return 0


def _speak_example(project: str | None, player: object | None, sleep, engine: object | None,
                   settings: object | None) -> int:
    from quill import speech

    if settings is None:
        from quill.config import ConfigError, load_config

        try:
            settings = load_config().claude_alert
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
    name = speech.spoken(project if project is not None else speech.EXAMPLE_NAME)
    if not name:
        print("the project name has nothing to speak", file=sys.stderr)
        return 2
    player = WinsoundPlayer() if player is None else player
    engine = speech.PowerShellSpeech() if engine is None else engine
    print(f"playing the '{DONE}' sound ({ALIASES[DONE]}), then the project name")
    player.play(DONE)
    sleep(speech.CHIME_GAP_S)
    process = None
    try:
        process = engine.start(speech.payload([name], settings.speech_rate, settings.speech_volume))
        code = process.wait(timeout=speech.SPEAK_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - reported by type only
        if process is not None:
            process.terminate()
        print(f"{NO_VOICE} ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        player.stop()
    if code != 0:
        print(NO_VOICE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
