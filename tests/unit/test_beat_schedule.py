"""reel's beat-timing: the fixed fallback schedule, and the librosa-optional
beat schedule (which must degrade gracefully to None)."""

import math
import sys
import wave

import numpy as np
import pytest

from image_tools import reel


# --- fixed fallback -------------------------------------------------------

def test_fixed_schedule_is_evenly_spaced():
    assert reel._fixed_schedule(4, 2.0) == [0.0, 2.0, 4.0, 6.0]


def test_fixed_schedule_single_transition():
    assert reel._fixed_schedule(1, 2.0) == [0.0]


# --- librosa-missing fallback ---------------------------------------------

def test_beat_schedule_none_when_librosa_missing(monkeypatch, capture_ctx):
    # Force `import librosa` inside the function to raise ImportError.
    monkeypatch.setitem(sys.modules, "librosa", None)
    result = reel._compute_beat_schedule("whatever.mp3", 4, 4, 0.5, capture_ctx)
    assert result is None
    assert any("librosa not installed" in line for line in capture_ctx.logs)


# --- real librosa path (optional dependency) ------------------------------

def _write_sine_wav(path, seconds=1.0, sr=22050, freq=220.0):
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    samples = (0.5 * np.sin(2 * math.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


@pytest.mark.librosa
def test_beat_schedule_runs_with_real_librosa(tmp_path, capture_ctx):
    wav = tmp_path / "tone.wav"
    _write_sine_wav(wav)
    result = reel._compute_beat_schedule(str(wav), 2, 4, 0.5, capture_ctx)
    # A featureless tone may have too few beats (-> None) or produce a
    # schedule; either way the call must succeed and return the right shape.
    assert result is None or all(isinstance(x, float) for x in result)
