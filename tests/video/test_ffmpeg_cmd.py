"""Mock-ffmpeg tests: run the full frame pipeline with subprocess.Popen faked,
then assert on the ffmpeg command line and that frames were actually produced.
No ffmpeg binary is involved.
"""

import pytest

from image_tools import reel, scroll_video, shuffle_reveal


def _res(cmd):
    """The 'WIDTHxHEIGHT' string passed to ffmpeg's -s flag."""
    return cmd[cmd.index("-s") + 1]


def _assert_x264_output(cmd, output):
    assert cmd[0] == "ffmpeg"
    assert "libx264" in cmd
    assert "yuv420p" in cmd
    assert cmd[-1] == str(output)


def test_reel_builds_expected_command(tmp_path, image_folder, capture_ctx,
                                      fake_ffmpeg):
    folder = image_folder([(1000, 1500)] * 4)  # portraits -> one slide each
    out = tmp_path / "reel.mp4"
    result = reel.run(folder=str(folder), interval=0.1, output=str(out),
                      ctx=capture_ctx)

    assert result == str(out)
    assert len(fake_ffmpeg) == 1
    proc = fake_ffmpeg[0]
    _assert_x264_output(proc.cmd, out)
    assert _res(proc.cmd) == f"{reel.WIDTH}x{reel.HEIGHT}"
    assert "-an" in proc.cmd  # no music -> no audio stream
    assert proc.stdin.bytes_written > 0  # frames were piped


def test_reel_with_music_maps_audio(tmp_path, image_folder, capture_ctx,
                                    fake_ffmpeg):
    folder = image_folder([(1000, 1500)] * 3)
    track = tmp_path / "song.mp3"
    track.write_bytes(b"\x00")  # never decoded (ffmpeg is faked)
    out = tmp_path / "reel.mp4"
    reel.run(folder=str(folder), interval=0.1, music=str(track),
             output=str(out), ctx=capture_ctx)

    cmd = fake_ffmpeg[0].cmd
    assert "-an" not in cmd
    assert "aac" in cmd
    assert str(track) in cmd


def test_scroll_builds_expected_command(tmp_path, image_folder, capture_ctx,
                                        fake_ffmpeg):
    folder = image_folder([(1000, 1000)] * 4)
    out = tmp_path / "scroll.mp4"
    # High speed percent keeps the frame budget (and test time) tiny.
    scroll_video.run(folder=str(folder), aspect_ratio="9:16",
                     scroll_speed_pct=2000, output=str(out), ctx=capture_ctx)

    proc = fake_ffmpeg[0]
    _assert_x264_output(proc.cmd, out)
    assert _res(proc.cmd) == "1080x1920"
    assert "-an" in proc.cmd
    assert proc.stdin.bytes_written > 0


def test_shuffle_builds_expected_command(tmp_path, image_folder, capture_ctx,
                                         fake_ffmpeg):
    folder = image_folder([(1000, 1000)] * 3)
    out = tmp_path / "shuffle.mp4"
    shuffle_reveal.run(folder=str(folder), aspect_ratio="9:16", hold_s=0.1,
                       min_intermediate=0, max_intermediate=0, seed=0,
                       output=str(out), ctx=capture_ctx)

    proc = fake_ffmpeg[0]
    _assert_x264_output(proc.cmd, out)
    assert _res(proc.cmd) == "1080x1920"
    assert proc.stdin.bytes_written > 0
