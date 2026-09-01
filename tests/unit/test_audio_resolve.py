"""The `_resolve_audio_track` helper is duplicated verbatim across the video
tools; test them all against the same contract."""

import pytest

from image_tools import ken_burns, reel, rotate_video, scroll_video

MODULES = [reel, scroll_video, rotate_video, ken_burns]
IDS = [m.__name__.rsplit(".", 1)[-1] for m in MODULES]


@pytest.fixture(params=MODULES, ids=IDS)
def mod(request):
    return request.param


def test_file_with_known_extension_is_returned(mod, tmp_path, capture_ctx):
    track = tmp_path / "song.mp3"
    track.write_bytes(b"\x00")
    assert mod._resolve_audio_track(str(track), capture_ctx) == str(track)


def test_file_with_unknown_extension_still_returned_with_warning(
        mod, tmp_path, capture_ctx):
    track = tmp_path / "song.txt"
    track.write_bytes(b"\x00")
    assert mod._resolve_audio_track(str(track), capture_ctx) == str(track)
    assert any("not a known audio extension" in line
               for line in capture_ctx.logs)


def test_folder_with_audio_picks_a_member(mod, tmp_path, capture_ctx):
    folder = tmp_path / "music"
    folder.mkdir()
    track = folder / "a.mp3"
    track.write_bytes(b"\x00")
    assert mod._resolve_audio_track(str(folder), capture_ctx) == str(track)


def test_folder_without_audio_returns_none(mod, tmp_path, capture_ctx):
    folder = tmp_path / "empty"
    folder.mkdir()
    assert mod._resolve_audio_track(str(folder), capture_ctx) is None
    assert any("no audio files" in line.lower() for line in capture_ctx.logs)


def test_missing_path_raises(mod, tmp_path, capture_ctx):
    with pytest.raises(ValueError, match="not a file or folder"):
        mod._resolve_audio_track(str(tmp_path / "nope.mp3"), capture_ctx)
