"""A cancelled RunContext must stop each video render before it finishes,
with ffmpeg faked out.

Tools stop cancellation in one of two valid ways: reel checks the non-raising
`cancelled()` in its render loop and returns None, while scroll/shuffle hit the
raising `check_cancelled()` during image prep and surface RunCancelled. Either
way, no completed output path is returned.
"""

import pytest

from image_tools import RunCancelled, reel, scroll_video, shuffle_reveal


def _run_stopped(fn):
    """Run `fn`, treating a raised RunCancelled as a None result."""
    try:
        return fn()
    except RunCancelled:
        return None


def test_reel_cancelled_stops(tmp_path, image_folder, capture_ctx, fake_ffmpeg):
    folder = image_folder([(1000, 1500)] * 3)
    capture_ctx.cancel()
    result = _run_stopped(lambda: reel.run(
        folder=str(folder), interval=0.1,
        output=str(tmp_path / "r.mp4"), ctx=capture_ctx))
    assert result is None


def test_scroll_cancelled_stops(tmp_path, image_folder, capture_ctx,
                                fake_ffmpeg):
    folder = image_folder([(1000, 1000)] * 4)
    capture_ctx.cancel()
    result = _run_stopped(lambda: scroll_video.run(
        folder=str(folder), aspect_ratio="9:16", scroll_speed_pct=2000,
        output=str(tmp_path / "s.mp4"), ctx=capture_ctx))
    assert result is None


def test_shuffle_cancelled_stops(tmp_path, image_folder, capture_ctx,
                                 fake_ffmpeg):
    folder = image_folder([(1000, 1000)] * 3)
    capture_ctx.cancel()
    result = _run_stopped(lambda: shuffle_reveal.run(
        folder=str(folder), aspect_ratio="9:16", hold_s=0.1,
        min_intermediate=0, max_intermediate=0, seed=0,
        output=str(tmp_path / "sh.mp4"), ctx=capture_ctx))
    assert result is None
