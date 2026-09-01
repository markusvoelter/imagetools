"""Shared fixtures for the Image Tools test suite.

Test inputs are *generated at runtime* (see `make_image` / `image_folder`)
rather than committed as binary files: the tools care about geometry
(dimensions, aspect ratios, counts), which solid-colour / gradient PIL images
exercise fully. Everything lands under pytest's `tmp_path`, so the real
project `assets/` and `output/` folders are never touched.
"""

import shutil

import pytest
from PIL import Image

from image_tools import RunContext


# --------------------------------------------------------------------------
#  Image + folder generation
# --------------------------------------------------------------------------

@pytest.fixture
def make_image():
    """Factory: `make_image(w, h, color=...)` -> an RGB PIL image.

    A per-call colour derived from the size keeps generated images visually
    distinct (useful for content-sensitive code paths) while staying fully
    deterministic.
    """
    def _make(w, h, color=None):
        if color is None:
            color = (w % 256, h % 256, (w + h) % 256)
        return Image.new("RGB", (w, h), color)

    return _make


@pytest.fixture
def image_folder(tmp_path, make_image):
    """Factory: write images of the given sizes into a folder.

    `image_folder(sizes, ext="jpg", subdir=None)` where `sizes` is a list of
    `(w, h)` tuples. Returns the folder `Path`; files are named
    `img_00.<ext>`, `img_01.<ext>`, ... in sorted order.
    """
    def _make(sizes, ext="jpg", subdir="imgs"):
        folder = tmp_path / subdir if subdir else tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        save_kwargs = {"quality": 90} if ext.lower() in ("jpg", "jpeg") else {}
        for i, (w, h) in enumerate(sizes):
            path = folder / f"img_{i:02d}.{ext}"
            make_image(w, h).save(path, **save_kwargs)
        return folder

    return _make


# --------------------------------------------------------------------------
#  Run context that captures log output
# --------------------------------------------------------------------------

@pytest.fixture
def capture_ctx():
    """A `RunContext` whose log lines are captured on `ctx.logs` (a list)."""
    logs = []
    ctx = RunContext(log=logs.append)
    ctx.logs = logs
    return ctx


# --------------------------------------------------------------------------
#  External-dependency gates
# --------------------------------------------------------------------------

def _has(binary):
    return shutil.which(binary) is not None


@pytest.fixture(scope="session")
def ffmpeg_available():
    return _has("ffmpeg") and _has("ffprobe")


def pytest_collection_modifyitems(config, items):
    """Auto-skip `ffmpeg`/`librosa`-marked tests when the dependency is absent."""
    have_ffmpeg = _has("ffmpeg") and _has("ffprobe")
    try:
        import librosa  # noqa: F401
        have_librosa = True
    except Exception:
        have_librosa = False

    skip_ffmpeg = pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH")
    skip_librosa = pytest.mark.skip(reason="librosa not installed")
    for item in items:
        if "ffmpeg" in item.keywords and not have_ffmpeg:
            item.add_marker(skip_ffmpeg)
        if "librosa" in item.keywords and not have_librosa:
            item.add_marker(skip_librosa)
