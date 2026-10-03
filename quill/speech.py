"""The project names of the Claude Code alerts, spoken after the chime with a voice Windows already has,
or played from the user's own recording of the name.

Nothing is installed and nothing leaves the PC: ``PowerShellSpeech`` starts the
built-in Windows PowerShell 5.1 (at its absolute ``%SystemRoot%`` path, with
``-NoProfile -NonInteractive``, no console window and no focus change) with a
constant script (``SCRIPT``). The request reaches it only on stdin, as UTF-8
JSON, never in the command line or the script text: segments played one after
another, each either names to speak (plain text, never markup, at the rate and
volume of the request) or a recorded clip (base64 WAV bytes, already scaled to
the volume here). The script prefers a pt-PT voice of
``Windows.Media.SpeechSynthesis`` (the OneCore voices), then another pt voice,
then the default WinRT voice, then the ``System.Speech`` default voice. A
segment that cannot be synthesized or played is skipped silently: the chime
has already played and the indicator still shows the names.

With own voice on, ``Speaker`` has a clip store (``quill.clips.ClipStore``):
when a request starts (on the worker thread, never under the session lock) it
looks up each name's clip by ``quill.clips.clip_key``; a name with a valid
clip plays the clip, the others keep the Windows voice, in the same order. A
clip that cannot be used falls back to the voice and logs a reason code only.

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
import base64
import io
import json
import logging
import math
import re
import subprocess
import sys
import threading
import time
import wave
from array import array
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
$segments = @($request.segments)
if ($segments.Count -eq 0) { exit 1 }
$rate = [double]$request.rate
$volume = [int]$request.volume
$out = @{ bytes = $null; language = '' }
function Synthesize([string]$text) {
    $out.bytes = $null
    $out.language = ''
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
        if ($wave.Length -gt 0) { $out.bytes = $wave.ToArray(); $out.language = $voice.Language }
    } catch { $out.bytes = $null }
    if ($null -eq $out.bytes) {
        try {
            Add-Type -AssemblyName System.Speech
            $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
            $synth.Rate = [int][Math]::Max(-10, [Math]::Min(10, [Math]::Round(10 * [Math]::Log($rate) / [Math]::Log(3))))
            $synth.Volume = $volume
            $wave = New-Object IO.MemoryStream
            $synth.SetOutputToWaveStream($wave)
            $synth.Speak($text)
            $synth.SetOutputToNull()
            if ($wave.Length -gt 0) { $out.bytes = $wave.ToArray(); $out.language = $synth.Voice.Culture.Name }
        } catch { $out.bytes = $null }
    }
}
$failed = $false
$language = ''
$size = 0
foreach ($segment in $segments) {
    $bytes = $null
    if ($null -ne $segment.wav) {
        try { $bytes = [Convert]::FromBase64String([string]$segment.wav) } catch { $bytes = $null }
    } else {
        $text = [string]::Join(', ', [string[]]@($segment.say))
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            Synthesize $text
            $bytes = $out.bytes
            if ($null -ne $bytes) { $language = $out.language; $size += $bytes.Length }
        }
    }
    if ($null -eq $bytes) { $failed = $true; continue }
    try {
        $player = New-Object Media.SoundPlayer -ArgumentList (,(New-Object IO.MemoryStream -ArgumentList (,$bytes)))
        if ($request.probe) { $player.Load() } else { $player.PlaySync() }
    } catch { $failed = $true }
}
if ($failed) { exit 1 }
if ($request.probe) { [Console]::Out.Write($language + ' ' + $size) }
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


def segments(items: Sequence[str | bytes]) -> list[dict[str, object]]:
    """The script's segments, in order: consecutive names (``str``) are spoken together
    (``{"say": [...]}``), a clip (WAV ``bytes``) is played (``{"wav": base64}``)."""
    result: list[dict[str, object]] = []
    for item in items:
        if isinstance(item, bytes):
            result.append({"wav": base64.b64encode(item).decode("ascii")})
        elif result and "say" in result[-1]:
            result[-1]["say"].append(item)
        else:
            result.append({"say": [item]})
    return result


def payload(items: Sequence[str | bytes], rate: float, volume: int, probe: bool = False) -> bytes:
    """The UTF-8 JSON the script reads from stdin; ``items`` are names to speak or clip WAV bytes."""
    return json.dumps({"segments": segments(items), "rate": float(rate), "volume": int(volume), "probe": probe},
                      ensure_ascii=False).encode("utf-8")


def clip_wav(clip: object, volume: int) -> bytes:
    """A clip (``quill.clips.Clip``: mono PCM16 samples and their rate) as WAV bytes, scaled by ``volume``/100."""
    samples = array("h")
    samples.frombytes(clip.pcm)
    factor = max(VOLUME_RANGE[0], min(VOLUME_RANGE[1], int(volume))) / 100.0
    scaled = array("h", (max(-32768, min(32767, round(sample * factor))) for sample in samples))
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(clip.rate)
        handle.writeframes(scaled.tobytes())
    return out.getvalue()


def request(names: Sequence[str], rate: float, volume: int, clips: object | None = None) -> bytes:
    """The payload of ``names``: a name with a valid clip in ``clips`` (``quill.clips.ClipStore``;
    None: no clips) plays it, the others are spoken. Reads files: never call it under the session lock."""
    items: list[str | bytes] = list(names)
    if clips is not None and items:
        try:
            found = clips.clips_for(list(names))
            if len(found) != len(items):
                raise ValueError("one clip lookup per name")
            items = [name if clip is None else clip_wav(clip, volume) for name, clip in zip(names, found)]
        except Exception as exc:  # noqa: BLE001 - every name keeps the Windows voice
            log.error("own-voice clips not used (%s)", type(exc).__name__)
            items = list(names)
    return payload(items, rate, volume)


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
        """Start speaking ``data`` (``payload``); returns at once with the process. A short thread
        writes ``data`` to its stdin (clips make it larger than a pipe holds), so the caller never
        waits for PowerShell to read it and may terminate the process meanwhile."""
        process = subprocess.Popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                                   startupinfo=_hidden(), close_fds=True)
        threading.Thread(target=_feed, args=(process, data), name="quill-speech-input", daemon=True).start()
        return process

    def probe(self, data: bytes, timeout_s: float = PROBE_TIMEOUT_S) -> tuple[int, str]:
        """Run a probe request to its end: (exit code, stdout)."""
        done = subprocess.run(self.args, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW, startupinfo=_hidden(), timeout=timeout_s,
                              close_fds=True)
        return done.returncode, done.stdout[:MAX_PROBE_OUTPUT].decode("ascii", "replace")


def _feed(process: subprocess.Popen, data: bytes) -> None:
    """Write the request to the process's stdin; a failed write (it was terminated) kills it."""
    try:
        process.stdin.write(data)
        process.stdin.close()
    except (OSError, ValueError) as exc:
        log.warning("speech input failed (%s)", type(exc).__name__)
        try:
            process.kill()
        except OSError:
            pass


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
    request, driven by the fake ``clock``. ``clips`` (``quill.clips.ClipStore``;
    None: always the Windows voice) gives the own-voice clips of the names,
    read when a request starts, so a newly recorded clip is used at once.
    """

    def __init__(self, engine: object, *, rate: float = DEFAULT_RATE, volume: int = DEFAULT_VOLUME,
                 gap_s: float = CHIME_GAP_S, clock: Callable[[], float] = time.monotonic,
                 threaded: bool = True, clips: object | None = None) -> None:
        self.engine = engine
        self.clips = clips
        self.rate = rate
        self.volume = volume
        self.gap_s = gap_s
        self.clock = clock
        self.threaded = threaded
        self._lock = threading.Condition()
        self._generation = 0
        self._pending: tuple[float, tuple[str, ...], int] | None = None
        self._process: object | None = None
        self._waiting = False  # a worker thread is waiting for the pending request (set and cleared under the lock)

    def say(self, names: Sequence[str]) -> None:
        """Speak ``names`` ``gap_s`` from now (the chime just started); a speech still going stops.
        Never blocks and reads no file: the clips are read and the process starts later, on another thread."""
        names = tuple(name for name in names if name)[:MAX_NAMES]
        with self._lock:
            self._generation += 1
            process, self._process = self._process, None
            self._pending = (self.clock() + self.gap_s, names, self._generation) if names else None
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
            _, names, generation = self._pending
            self._pending = None
        data = request(names, self.rate, self.volume, self.clips)
        with self._lock:
            if generation != self._generation:  # a hold (or a newer alert) came while the clips were read
                return False
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


def real_speaker(rate: float, volume: int, clips: object | None = None) -> Speaker | None:
    """The app's speaker with the real engine and the own-voice ``clips`` (None: the Windows voice only),
    or None (only the chime) when PowerShell cannot be located."""
    try:
        return Speaker(PowerShellSpeech(), rate=rate, volume=volume, clips=clips)
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
