"""MME capture for the benchmark recorder: re-exported from the product.

The capture code lives in ``quill.audio`` so the recorder and the app share
one implementation (MME at 16 kHz mono PCM16, never DirectSound). This module
keeps the names the benchmark has always imported.
"""

from quill.audio import (  # noqa: F401
    BYTES_PER_SECOND,
    CHANNELS,
    CLOCK_TOLERANCE_RATIO,
    CLOCK_TOLERANCE_S,
    FRAME_S,
    MAX_ZERO_RUN_S,
    MIN_SPEECH_RMS,
    MIN_TAKE_S,
    MIN_TRUNCATED_NAME,
    MME_NAME_LIMIT,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioError,
    Capture,
    CaptureResult,
    MmeBuffer,
    WinMM,
    input_devices,
    longest_zero_run_s,
    name_matches,
    select_device,
    speech_level,
    take_problem,
)
