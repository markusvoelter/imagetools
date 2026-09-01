"""Fixtures for the video-tool tests.

Two tiers live here:
  * `fake_ffmpeg` -- monkeypatches subprocess.Popen so the render pipeline runs
    without ffmpeg; frames are discarded but the argv and byte-count are
    recorded. Fast, always runs.
  * `ffprobe_streams` -- inspects a real rendered file; used by the
    `@pytest.mark.ffmpeg` smoke tests only.
"""

import json
import subprocess

import pytest


class _StdinSink:
    """A stand-in for a piped ffmpeg stdin: counts bytes, discards them."""

    def __init__(self):
        self.bytes_written = 0
        self.closed = False

    def write(self, b):
        self.bytes_written += len(b)
        return len(b)

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, cmd):
        self.cmd = cmd
        self.stdin = _StdinSink()
        self.returncode = 0
        self.terminated = False

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Patch subprocess.Popen; return the list of spawned fake processes.

    Every tool does `import subprocess; subprocess.Popen(...)`, so patching the
    shared module object covers all of them.
    """
    procs = []

    def _popen(cmd, *args, **kwargs):
        proc = _FakeProc(cmd)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return procs


@pytest.fixture
def ffprobe_streams():
    """Return a callable: path -> list of ffprobe stream dicts."""
    def _probe(path):
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ])
        return json.loads(out)

    return _probe
