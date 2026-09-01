"""Tests for rotate_video.

Unlike the frame-piping tools, rotate_video hands ffmpeg a `-filter_complex`
graph and reads its stdout line-by-line, so it needs a fake process that
exposes an iterable `stdout` rather than a `stdin` sink. The overlay/endscreen
PNG assets are redirected to generated temp files so the test is independent of
the repo's `assets/` folder.
"""

import subprocess

import pytest

from image_tools import rotate_video


@pytest.fixture
def fake_ffmpeg_lines(monkeypatch):
    """Patch subprocess.Popen with a process whose stdout yields a few log
    lines and which exits 0. Returns the list of spawned fake processes."""
    procs = []

    class _Proc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.stdout = iter(["frame= 1\n", "frame= 2\n"])
            self.returncode = 0

        def wait(self):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

    def _popen(cmd, *a, **k):
        p = _Proc(cmd)
        procs.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return procs


@pytest.fixture
def rotate_assets(monkeypatch, make_image, tmp_path):
    """Point OVERLAY_PNG / ENDSCREEN_PNG at generated temp files."""
    overlay = tmp_path / "overlay.png"
    endscreen = tmp_path / "endscreen.png"
    make_image(540, 540).save(overlay)
    make_image(1920, 1080).save(endscreen)
    monkeypatch.setattr(rotate_video, "OVERLAY_PNG", str(overlay))
    monkeypatch.setattr(rotate_video, "ENDSCREEN_PNG", str(endscreen))


def _folder_with_cover(image_folder, make_image, tmp_path, n_horizontal=3):
    folder = image_folder([(1600, 1000)] * n_horizontal, subdir="rot")
    cover = folder / "cover.jpg"
    make_image(1080, 1920).save(cover)
    return folder


def test_rotate_builds_command(image_folder, make_image, tmp_path, capture_ctx,
                               rotate_assets, fake_ffmpeg_lines):
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    out = tmp_path / "rot.mp4"
    result = rotate_video.run(folder=str(folder), total_duration_seconds=10,
                              cover_image="cover.jpg", output=str(out),
                              ctx=capture_ctx)
    assert result == str(out)
    assert len(fake_ffmpeg_lines) == 1
    cmd = fake_ffmpeg_lines[0].cmd
    assert cmd[0] == "ffmpeg"
    assert "libx264" in cmd
    assert "-filter_complex" in cmd
    assert cmd[-1] == str(out)


def test_rotate_single_horizontal(image_folder, make_image, tmp_path,
                                  capture_ctx, rotate_assets, fake_ffmpeg_lines):
    folder = _folder_with_cover(image_folder, make_image, tmp_path,
                                n_horizontal=1)
    out = tmp_path / "rot.mp4"
    rotate_video.run(folder=str(folder), total_duration_seconds=8,
                     cover_image="cover.jpg", output=str(out), ctx=capture_ctx)
    assert len(fake_ffmpeg_lines) == 1


def test_rotate_with_music_file(image_folder, make_image, tmp_path,
                                capture_ctx, rotate_assets, fake_ffmpeg_lines):
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    track = tmp_path / "song.mp3"
    track.write_bytes(b"\x00")
    out = tmp_path / "rot.mp4"
    rotate_video.run(folder=str(folder), total_duration_seconds=10,
                     cover_image="cover.jpg", music=str(track), output=str(out),
                     ctx=capture_ctx)
    cmd = fake_ffmpeg_lines[0].cmd
    assert str(track) in cmd
    assert "aac" in cmd


def test_rotate_with_music_folder(image_folder, make_image, tmp_path,
                                  capture_ctx, rotate_assets, fake_ffmpeg_lines):
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "a.wav").write_bytes(b"\x00")
    out = tmp_path / "rot.mp4"
    rotate_video.run(folder=str(folder), total_duration_seconds=10,
                     cover_image="cover.jpg", music=str(music_dir),
                     output=str(out), ctx=capture_ctx)
    assert str(music_dir / "a.wav") in fake_ffmpeg_lines[0].cmd


def test_rotate_default_output_path(image_folder, make_image, tmp_path,
                                    capture_ctx, rotate_assets,
                                    fake_ffmpeg_lines):
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    result = rotate_video.run(folder=str(folder), total_duration_seconds=10,
                              cover_image="cover.jpg", ctx=capture_ctx)
    assert result.endswith(".mp4")


# --------------------------------------------------------------------------
#  Error / validation paths
# --------------------------------------------------------------------------

def test_rotate_bad_folder_raises(capture_ctx):
    with pytest.raises(ValueError):
        rotate_video.run(folder="/no/such/dir", total_duration_seconds=10,
                         cover_image="cover.jpg", ctx=capture_ctx)


def test_rotate_duration_too_short_raises(image_folder, make_image, tmp_path,
                                          capture_ctx, rotate_assets):
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    with pytest.raises(ValueError):
        rotate_video.run(folder=str(folder), total_duration_seconds=1,
                         cover_image="cover.jpg", ctx=capture_ctx)


def test_rotate_missing_cover_raises(image_folder, make_image, tmp_path,
                                     capture_ctx, rotate_assets):
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    with pytest.raises(ValueError):
        rotate_video.run(folder=str(folder), total_duration_seconds=10,
                         cover_image="missing.jpg", ctx=capture_ctx)


def test_rotate_no_horizontals_raises(make_image, tmp_path, capture_ctx,
                                      rotate_assets):
    folder = tmp_path / "onlycover"
    folder.mkdir()
    make_image(1080, 1920).save(folder / "cover.jpg")
    with pytest.raises(ValueError):
        rotate_video.run(folder=str(folder), total_duration_seconds=10,
                         cover_image="cover.jpg", ctx=capture_ctx)


def test_rotate_missing_overlay_asset_raises(image_folder, make_image, tmp_path,
                                             capture_ctx, monkeypatch):
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    monkeypatch.setattr(rotate_video, "OVERLAY_PNG", str(tmp_path / "nope.png"))
    with pytest.raises(FileNotFoundError):
        rotate_video.run(folder=str(folder), total_duration_seconds=10,
                         cover_image="cover.jpg", ctx=capture_ctx)


def test_rotate_ffmpeg_nonzero_raises(image_folder, make_image, tmp_path,
                                      capture_ctx, rotate_assets, monkeypatch):
    class _FailProc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.stdout = iter(["boom\n"])

        def wait(self):
            return 1

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: _FailProc(cmd))
    folder = _folder_with_cover(image_folder, make_image, tmp_path)
    with pytest.raises(RuntimeError):
        rotate_video.run(folder=str(folder), total_duration_seconds=10,
                         cover_image="cover.jpg", output=str(tmp_path / "o.mp4"),
                         ctx=capture_ctx)
