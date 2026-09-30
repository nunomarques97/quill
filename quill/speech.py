"""The project names of the Claude Code alerts, spoken after the chime with a voice Windows already has.

Nothing is installed and nothing leaves the PC: ``PowerShellSpeech`` starts the
built-in Windows PowerShell 5.1 (at its absolute ``%SystemRoot%`` path, with
``-NoProfile -NonInteractive``, no console window and no focus change) with a
constant script (``SCRIPT``). The names, the rate and the volume reach it only
on stdin, as UTF-8 JSON, never in the command line or the script text, and are
spoken as plain text (never markup). The script prefers a pt-PT voice of
``Windows.Media.SpeechSynthesis`` (the OneCore voices), then another pt voice,
then the default WinRT voice, then the ``System.Speech`` default voice. When
no voice is available or synthesis or playback fails it exits silently: the
chime has already played and the indicator still shows the names.

``Speaker`` decides when: ``say(names)`` schedules the speech ``gap_s`` after
the chime starts (``CHIME_GAP_S``), and ``stop()`` cancels a pending speech
and terminates a speaking process without waiting (the sessions call it under
their lock when a hold starts, so speaking never reaches the microphone). Only
the latest request may start a process: a request that loses a race with
``stop`` terminates the process it just started. Logs hold exception types
only, never a name.

Tests never create a ``PowerShellSpeech``: importing ``quill.tests`` makes its
constructor fail. Manual commands (nothing is played by the probe):

    py -3.12 -m quill.speech --probe          synthesize an invented name in memory
    py -3.12 -m quill.sound --play-sound --speak   hear the chime and an invented name
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from quill import sound

log = logging.getLogger("quill.speech")

# The name starts no earlier than this after the chime starts (the chime lasts about a second).
CHIME_GAP_S = 0.8
MAX_NAMES = 3
MAX_NAME_CHARS = 48
# WinRT SpeechSynthesizerOptions: SpeakingRate 0.5-6.0 (1.0 is normal speed), AudioVolume 0-1 (here 0-100).
RATE_RANGE = (0.5, 6.0)
VOLUME_RANGE = (0, 100)
DEFAULT_RATE = 1.0
DEFAULT_VOLUME = 80
# An invented project name for the manual example and the probe.
EXAMPLE_NAME = "projeto-exemplo"
PROBE_TIMEOUT_S = 30.0
SPEAK_TIMEOUT_S = 30.0
MAX_PROBE_OUTPUT = 200
PROBE_LINE = re.compile(r"^([A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8}){0,3}) ([0-9]{1,12})$")

# Spoken as spaces, so "alpha-beta_gamma.io" reads as words.
_SEPARATORS = str.maketrans({"-": " ", "_": " ", ".": " "})

CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0

# The constant script: everything it speaks comes from stdin.
SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$input_stream = [Console]::OpenStandardInput()
$buffer = New-Object IO.MemoryStream
$input_stream.CopyTo($buffer)
$request = [Text.Encoding]::UTF8.GetString($buffer.ToArray()) | ConvertFrom-Json
$text = [string]::Join(', ', [string[]]@($request.names))
if ([string]::IsNullOrWhiteSpace($text)) { exit 1 }
$rate = [double]$request.rate
$volume = [int]$request.volume
$bytes = $null
$language = ''
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
    $as_task = [WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
    $voices = @([Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices)
    $voice = $voices | Where-Object { $_.Language -eq 'pt-PT' } | Select-Object -First 1
    if (-not $voice) { $voice = $voices | Where-Object { $_.Language -like 'pt-*' } | Select-Object -First 1 }
    if (-not $voice) { $voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::DefaultVoice }
    if (-not $voice) { throw 'no WinRT voice' }
    $synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
    $synth.Voice = $voice
    $synth.Options.SpeakingRate = $rate
    $synth.Options.AudioVolume = $volume / 100.0
    $task = $as_task.MakeGenericMethod([Windows.Media.SpeechSynthesis.SpeechSynthesisStream]).Invoke($null, @($synth.SynthesizeTextToStreamAsync($text)))
    $stream = $task.GetAwaiter().GetResult()
    $wave = New-Object IO.MemoryStream
    [IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream).CopyTo($wave)
    if ($wave.Length -gt 0) { $bytes = $wave.ToArray(); $language = $voice.Language }
} catch { $bytes = $null }
if ($null -eq $bytes) {
    try {
        Add-Type -AssemblyName System.Speech
        $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
        $synth.Rate = [int][Math]::Max(-10, [Math]::Min(10, [Math]::Round(10 * [Math]::Log($rate) / [Math]::Log(3))))
        $synth.Volume = $volume
        $wave = New-Object IO.MemoryStream
        $synth.SetOutputToWaveStream($wave)
        $synth.Speak($text)
        $synth.SetOutputToNull()
        if ($wave.Length -gt 0) { $bytes = $wave.ToArray(); $language = $synth.Voice.Culture.Name }
    } catch { $bytes = $null }
}
if ($null -eq $bytes) { exit 1 }
if ($request.probe) { [Console]::Out.Write($language + ' ' + $bytes.Length); exit 0 }
try {
    $player = New-Object Media.SoundPlayer -ArgumentList (,(New-Object IO.MemoryStream -ArgumentList (,$bytes)))
    $player.PlaySync()
} catch { exit 1 }
exit 0
""".strip()


def spoken(name: str) -> str:
    """How a project name is spoken: separators as spaces, whitespace collapsed, control characters dropped."""
    kept = "".join(" " if char.isspace() or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F else char
                   for char in name.translate(_SEPARATORS))
    return " ".join(kept.split())[:MAX_NAME_CHARS].strip()


def spoken_names(alerts: Iterable[tuple[str, str | None]]) -> list[str]:
    """The names to speak for the (kind, project) alerts: permission requests first, each name
    once, at most ``MAX_NAMES``; nameless alerts speak nothing."""
    ordered = sorted(alerts, key=lambda entry: entry[0] != sound.PERMISSION)  # stable: arrival order kept
    names: list[str] = []
    for _, project in ordered:
        text = spoken(project) if isinstance(project, str) else ""
        if text and text not in names:
            names.append(text)
        if len(names) == MAX_NAMES:
            break
    return names


def payload(names: Sequence[str], rate: float, volume: int, probe: bool = False) -> bytes:
    """The UTF-8 JSON the script reads from stdin."""
    return json.dumps({"names": list(names), "rate": float(rate), "volume": int(volume), "probe": probe},
                      ensure_ascii=False).encode("utf-8")


def system_root() -> Path:
    """%SystemRoot% as Windows reports it (not the environment variable, which a caller may change)."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("GetSystemWindowsDirectoryW failed")
    return Path(buffer.value)


def powershell_path(root: Path) -> Path:
    path = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_absolute():
        raise OSError("the Windows folder is not an absolute path")
    return path


def command_line(powershell: Path) -> list[str]:
    """The fixed command: the executable, the flags and the constant script; never a name."""
    return [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", SCRIPT]


def _hidden() -> subprocess.STARTUPINFO:
    info = subprocess.STARTUPINFO()
    info.dwFlags |= STARTF_USESHOWWINDOW
    info.wShowWindow = SW_HIDE
    return info


class PowerShellSpeech:
    """The real engine: a hidden Windows PowerShell 5.1 child per request."""

    def __init__(self, root: Path | None = None) -> None:
        self.args = command_line(powershell_path(system_root() if root is None else root))

    def start(self, data: bytes) -> subprocess.Popen:
        """Start speaking ``data`` (``payload``); returns at once with the process."""
        process = subprocess.Popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                                   startupinfo=_hidden(), close_fds=True)
        try:
            process.stdin.write(data)
            process.stdin.close()
        except OSError:
            process.kill()
            raise
        return process

    def probe(self, data: bytes, timeout_s: float = PROBE_TIMEOUT_S) -> tuple[int, str]:
        """Run a probe request to its end: (exit code, stdout)."""
        done = subprocess.run(self.args, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW, startupinfo=_hidden(), timeout=timeout_s,
                              close_fds=True)
        return done.returncode, done.stdout[:MAX_PROBE_OUTPUT].decode("ascii", "replace")


def _terminate(process: object) -> None:
    try:
        process.terminate()
    except Exception as exc:  # noqa: BLE001 - it may have ended already
        log.error("speech stop failed (%s)", type(exc).__name__)


class Speaker:
    """Speaks the names after the chime, and stops at once when a dictation needs the microphone.

    ``engine.start(data)`` starts a process (``PowerShellSpeech``; tests pass a
    fake) that has ``terminate()``. With ``threaded`` a short worker thread per
    request waits for the gap; without it (tests) ``tick()`` starts a due
    request, driven by the fake ``clock``.
    """

    def __init__(self, engine: object, *, rate: float = DEFAULT_RATE, volume: int = DEFAULT_VOLUME,
                 gap_s: float = CHIME_GAP_S, clock: Callable[[], float] = time.monotonic,
                 threaded: bool = True) -> None:
        self.engine = engine
        self.rate = rate
        self.volume = volume
        self.gap_s = gap_s
        self.clock = clock
        self.threaded = threaded
        self._lock = threading.Condition()
        self._generation = 0
        self._pending: tuple[float, bytes, int] | None = None
        self._process: object | None = None
        self._waiting = False  # a worker thread is waiting for the pending request (set and cleared under the lock)

    def say(self, names: Sequence[str]) -> None:
        """Speak ``names`` ``gap_s`` from now (the chime just started); a speech still going stops.
        Never blocks: the process starts later, on another thread."""
        names = [name for name in names if name][:MAX_NAMES]
        with self._lock:
            self._generation += 1
            process, self._process = self._process, None
            self._pending = (self.clock() + self.gap_s, payload(names, self.rate, self.volume),
                             self._generation) if names else None
            self._lock.notify_all()
            if self._pending is not None and self.threaded and not self._waiting:
                self._waiting = True
                threading.Thread(target=self._wait, name="quill-speech", daemon=True).start()
        if process is not None:
            _terminate(process)

    def stop(self) -> None:
        """Cancel a pending speech and terminate a speaking process, without waiting for it."""
        with self._lock:
            self._generation += 1
            self._pending = None
            process, self._process = self._process, None
            self._lock.notify_all()
        if process is not None:
            _terminate(process)

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def tick(self) -> bool:
        """Start the pending speech when its time has come; True when a process was started and kept."""
        with self._lock:
            if self._pending is None or self.clock() < self._pending[0]:
                return False
            _, data, generation = self._pending
            self._pending = None
        try:
            process = self.engine.start(data)
        except Exception as exc:  # noqa: BLE001 - the chime played and the indicator shows the names
            log.error("speech start failed (%s)", type(exc).__name__)
            return False
        with self._lock:
            current = generation == self._generation
            if current:
                self._process = process
        if not current:  # a hold (or a newer alert) came while it was starting
            _terminate(process)
        return current

    def _wait(self) -> None:
        while True:
            with self._lock:
                if self._pending is None:
                    self._waiting = False  # a later say() starts a new thread
                    return
                left = self._pending[0] - self.clock()
                if left > 0:
                    self._lock.wait(left)
                    continue
            self.tick()


def real_speaker(rate: float, volume: int) -> Speaker | None:
    """The app's speaker with the real engine, or None (only the chime) when PowerShell cannot be located."""
    try:
        return Speaker(PowerShellSpeech(), rate=rate, volume=volume)
    except OSError as exc:
        log.error("speech unavailable (%s)", type(exc).__name__)
        return None


def rate_to_sapi(rate: float) -> int:
    """The System.Speech rate (-10..10, where 10 is about three times faster) of a WinRT rate (for tests)."""
    return max(-10, min(10, round(10 * math.log(rate) / math.log(3))))


def probe(engine: object | None = None) -> tuple[str, int] | None:
    """Synthesize the invented example name in memory: (voice language, byte count), or None."""
    engine = PowerShellSpeech() if engine is None else engine
    try:
        code, output = engine.probe(payload([spoken(EXAMPLE_NAME)], DEFAULT_RATE, DEFAULT_VOLUME, probe=True))
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("speech probe failed (%s)", type(exc).__name__)
        return None
    match = PROBE_LINE.match(output.strip())
    if code != 0 or match is None or int(match.group(2)) <= 0:
        return None
    return match.group(1), int(match.group(2))


def main(argv: list[str] | None = None, engine: object | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.speech",
                                     description="Check the Windows voice of the spoken alert names (manual).")
    parser.add_argument("--probe", action="store_true",
                        help="synthesize an invented name in memory (plays nothing)")
    args = parser.parse_args(argv)
    if not args.probe:
        parser.print_usage(sys.stderr)
        return 2
    result = probe(engine)
    if result is None:
        print("speech probe: no Windows voice synthesized the example name")
        return 1
    language, size = result
    print(f"speech probe: voice {language}, {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
