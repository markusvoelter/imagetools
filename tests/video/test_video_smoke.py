"""End-to-end smoke tests that render real video with ffmpeg and inspect the
result with ffprobe. Gated on the ffmpeg binaries (auto-skipped otherwise) and
marked slow. Inputs and durations are kept tiny."""

import pytest

from image_tools import reel, scroll_video, shuffle_reveal

pytestmark = [pytest.mark.ffmpeg, pytest.mark.slow]


def _video_stream(meta):
    return next(s for s in meta["streams"] if s["codec_type"] == "video")


def test_reel_renders_playable_mp4(tmp_path, image_folder, capture_ctx,
                                   ffprobe_streams):
    folder = image_folder([(1000, 1500)] * 4)
    out = tmp_path / "reel.mp4"
    result = reel.run(folder=str(folder), interval=0.1, output=str(out),
                      ctx=capture_ctx)

    assert result == str(out) and out.stat().st_size > 0
    meta = ffprobe_streams(out)
    vs = _video_stream(meta)
    assert (vs["width"], vs["height"]) == (reel.WIDTH, reel.HEIGHT)
    assert float(meta["format"]["duration"]) > 0


def test_scroll_renders_playable_mp4(tmp_path, image_folder, capture_ctx,
                                     ffprobe_streams):
    folder = image_folder([(1000, 1000)] * 4)
    out = tmp_path / "scroll.mp4"
    scroll_video.run(folder=str(folder), aspect_ratio="9:16",
                     scroll_speed_pct=2000, output=str(out), ctx=capture_ctx)

    assert out.stat().st_size > 0
    vs = _video_stream(ffprobe_streams(out))
    assert (vs["width"], vs["height"]) == (1080, 1920)


def test_shuffle_renders_playable_mp4(tmp_path, image_folder, capture_ctx,
                                      ffprobe_streams):
    folder = image_folder([(1000, 1000)] * 3)
    out = tmp_path / "shuffle.mp4"
    shuffle_reveal.run(folder=str(folder), aspect_ratio="9:16", hold_s=0.1,
                       min_intermediate=0, max_intermediate=0, seed=0,
                       output=str(out), ctx=capture_ctx)

    assert out.stat().st_size > 0
    vs = _video_stream(ffprobe_streams(out))
    assert (vs["width"], vs["height"]) == (1080, 1920)
