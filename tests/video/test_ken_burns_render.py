"""Drive the full Ken Burns render pipeline with subprocess.Popen faked.

`ken_burns.run()` is the largest single function in the package; exercising it
end-to-end (frames are generated and piped to a fake ffmpeg, then discarded)
covers the compositing, timing, overlay and title/text-slide machinery without
needing the ffmpeg binary. Frame budgets are kept tiny (few images, sub-second
durations, small image dimensions are fine because output is fixed-size).
"""

import json

import pytest

from image_tools import ken_burns


def _landscape_folder(image_folder, n=6):
    # 16:9-ish source images -> pass the landscape orientation filter.
    return image_folder([(1600, 1000)] * n)


def _portrait_folder(image_folder, n=6):
    return image_folder([(1000, 1600)] * n)


def _assert_rendered(fake_ffmpeg, out):
    assert len(fake_ffmpeg) == 1
    proc = fake_ffmpeg[0]
    assert proc.cmd[0] == "ffmpeg"
    # Encoder is platform-dependent: h264_videotoolbox on macOS, libx264 else.
    assert "libx264" in proc.cmd or "h264_videotoolbox" in proc.cmd
    assert proc.cmd[-1] == str(out)
    assert proc.stdin.bytes_written > 0
    return proc


def test_basic_landscape(image_folder, capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    out = ken_burns.run(folder=str(folder), num_images=4, aspect="16:9",
                        duration_per_image=0.5, random_order=False,
                        ctx=capture_ctx)
    proc = _assert_rendered(fake_ffmpeg, out)
    assert "1920x1080" in proc.cmd
    assert out.endswith(".mp4")


def test_basic_portrait(image_folder, capture_ctx, fake_ffmpeg):
    folder = _portrait_folder(image_folder)
    out = ken_burns.run(folder=str(folder), num_images=4, aspect="9:16",
                        duration_per_image=0.5, random_order=False,
                        ctx=capture_ctx)
    proc = _assert_rendered(fake_ffmpeg, out)
    assert "1080x1920" in proc.cmd


def test_static_when_strength_zero(image_folder, capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    out = ken_burns.run(folder=str(folder), num_images=3, aspect="16:9",
                        duration_per_image=0.5, kb_strength=0.0,
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_title_and_subtitle(image_folder, capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    out = ken_burns.run(folder=str(folder), num_images=3, aspect="16:9",
                        duration_per_image=0.5, title="A Long Holiday Title",
                        subtitle="Summer 2026", random_order=False,
                        ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_text_slides_interspersed(image_folder, capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    out = ken_burns.run(folder=str(folder), num_images=5, aspect="16:9",
                        duration_per_image=0.4,
                        text_slides=["First interlude", "A slightly longer "
                                     "second interlude with more words"],
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_text_slides_portrait(image_folder, capture_ctx, fake_ffmpeg):
    folder = _portrait_folder(image_folder)
    out = ken_burns.run(folder=str(folder), num_images=4, aspect="9:16",
                        duration_per_image=0.4,
                        text_slides=["Portrait interlude text here"],
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_gimmick_intro(image_folder, capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder, n=8)
    out = ken_burns.run(folder=str(folder), num_images=6, aspect="16:9",
                        duration_per_image=0.4, gimmick=True,
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_end_screen(image_folder, make_image, tmp_path, capture_ctx,
                    fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    end = tmp_path / "end.jpg"
    make_image(1920, 1080).save(end)
    out = ken_burns.run(folder=str(folder), num_images=3, aspect="16:9",
                        duration_per_image=0.4, end_screen=str(end),
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_title_screen_image(image_folder, make_image, tmp_path, capture_ctx,
                            fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    title_bg = tmp_path / "titlebg.jpg"
    make_image(1920, 1080).save(title_bg)
    out = ken_burns.run(folder=str(folder), num_images=3, aspect="16:9",
                        duration_per_image=0.4, title="Overlaid Title",
                        title_screen=str(title_bg), random_order=False,
                        ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_project_name_output_stem(image_folder, capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    out = ken_burns.run(folder=str(folder), num_images=3, aspect="16:9",
                        duration_per_image=0.4, project_name="myproj",
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)
    assert out.endswith("myproj.mp4")


def test_music_without_sidecar(image_folder, tmp_path, capture_ctx,
                               fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    track = tmp_path / "song.mp3"
    track.write_bytes(b"\x00")  # never decoded (ffmpeg is faked)
    out = ken_burns.run(folder=str(folder), num_images=3, aspect="16:9",
                        duration_per_image=0.5, music=str(track),
                        random_order=False, ctx=capture_ctx)
    proc = _assert_rendered(fake_ffmpeg, out)
    assert str(track) in proc.cmd  # audio mapped


def _write_bar_sidecar(track, *, duration=6.0, step=0.5):
    """Write a `<track>.json` with regularly spaced bars and one of each event
    kind, so the bar-segment / overlay machinery is fully exercised."""
    n = int(duration / step)
    bars = [{"time": round(i * step, 3)} for i in range(1, n)]
    events = [
        {"time": 1.0, "kind": "flash"},
        {"time": 1.5, "kind": "glow"},
        {"time": 2.5, "kind": "quieting"},
        {"time": 3.0, "kind": "restart"},
        {"time": 3.5, "kind": "stop"},
        {"time": 4.0, "kind": "restart"},
        {"time": 4.5, "kind": "bleak",
         "params": {"brightness": -5, "contrast": 3, "color": "#102030",
                    "transparency": 40}},
        {"time": 5.5, "kind": "restart"},
    ]
    sidecar = track.with_suffix(".json")
    sidecar.write_text(json.dumps({"bars": bars, "events": events}))
    return sidecar


def test_music_with_bar_sidecar(image_folder, tmp_path, capture_ctx,
                                fake_ffmpeg):
    folder = _landscape_folder(image_folder, n=8)
    track = tmp_path / "beat.wav"
    track.write_bytes(b"\x00")
    _write_bar_sidecar(track)
    out = ken_burns.run(folder=str(folder), num_images=8, aspect="16:9",
                        duration_per_image=0.5, music=str(track),
                        random_order=False, ctx=capture_ctx)
    proc = _assert_rendered(fake_ffmpeg, out)
    assert str(track) in proc.cmd


def test_music_with_bar_sidecar_start_at_crop(image_folder, tmp_path,
                                              capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder, n=8)
    track = tmp_path / "beat.wav"
    track.write_bytes(b"\x00")
    sidecar = track.with_suffix(".json")
    data = json.loads(_write_bar_sidecar(track).read_text())
    data["events"].append({"time": 0.75, "kind": "crop"})
    sidecar.write_text(json.dumps(data))
    out = ken_burns.run(folder=str(folder), num_images=8, aspect="16:9",
                        duration_per_image=0.5, music=str(track),
                        start_at_crop=True, random_order=False,
                        ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_music_folder_picks_file(image_folder, tmp_path, capture_ctx,
                                 fake_ffmpeg):
    folder = _landscape_folder(image_folder)
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "only.mp3").write_bytes(b"\x00")
    out = ken_burns.run(folder=str(folder), num_images=3, aspect="16:9",
                        duration_per_image=0.5, music=str(music_dir),
                        random_order=False, ctx=capture_ctx)
    proc = _assert_rendered(fake_ffmpeg, out)
    assert str(music_dir / "only.mp3") in proc.cmd


def test_debug_overlay(image_folder, tmp_path, capture_ctx, fake_ffmpeg):
    folder = _landscape_folder(image_folder, n=6)
    track = tmp_path / "beat.wav"
    track.write_bytes(b"\x00")
    _write_bar_sidecar(track)
    out = ken_burns.run(folder=str(folder), num_images=6, aspect="16:9",
                        duration_per_image=0.5, music=str(track), debug=True,
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


def test_everything_at_once(image_folder, make_image, tmp_path, capture_ctx,
                            fake_ffmpeg):
    folder = _landscape_folder(image_folder, n=8)
    track = tmp_path / "beat.wav"
    track.write_bytes(b"\x00")
    _write_bar_sidecar(track)
    end = tmp_path / "end.jpg"
    make_image(1920, 1080).save(end)
    out = ken_burns.run(folder=str(folder), num_images=6, aspect="16:9",
                        duration_per_image=0.5, music=str(track),
                        gimmick=True, end_screen=str(end), title="Trip",
                        subtitle="2026", text_slides=["An interlude"],
                        random_order=False, ctx=capture_ctx)
    _assert_rendered(fake_ffmpeg, out)


# --------------------------------------------------------------------------
#  Validation / error paths
# --------------------------------------------------------------------------

def test_unknown_aspect_raises(image_folder, capture_ctx):
    folder = _landscape_folder(image_folder)
    with pytest.raises(ValueError):
        ken_burns.run(folder=str(folder), aspect="4:3", ctx=capture_ctx)


def test_num_images_below_one_raises(image_folder, capture_ctx):
    folder = _landscape_folder(image_folder)
    with pytest.raises(ValueError):
        ken_burns.run(folder=str(folder), num_images=0, ctx=capture_ctx)


def test_bad_duration_raises(image_folder, capture_ctx):
    folder = _landscape_folder(image_folder)
    with pytest.raises(ValueError):
        ken_burns.run(folder=str(folder), duration_per_image=0, ctx=capture_ctx)


def test_no_matching_images_raises(image_folder, capture_ctx):
    # Only portraits present, but we ask for landscape output.
    folder = _portrait_folder(image_folder)
    with pytest.raises(Exception):
        ken_burns.run(folder=str(folder), aspect="16:9", num_images=3,
                      duration_per_image=0.5, ctx=capture_ctx)
