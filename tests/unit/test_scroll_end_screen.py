"""The scroll-video end screen must fill the whole frame (cover + crop),
not be shrunk inside a padded area."""

from PIL import Image

from image_tools import scroll_video


def test_end_screen_frame_fills_canvas(tmp_path):
    # A source whose aspect differs from the target still fills the frame.
    src = tmp_path / "end.png"
    Image.new("RGB", (800, 800), (10, 200, 30)).save(src)

    frame = scroll_video._build_end_screen_frame(str(src), 1080, 1920)

    assert frame.size == (1080, 1920)
    # Cover-scaling leaves no background bars: the four corners are the image,
    # not the black end-screen background.
    for xy in [(0, 0), (1079, 0), (0, 1919), (1079, 1919), (540, 960)]:
        assert frame.getpixel(xy) != scroll_video.SCROLL_END_SCREEN_BG


def test_end_screen_frame_same_aspect_is_exact(tmp_path):
    src = tmp_path / "end.png"
    Image.new("RGB", (540, 960), (200, 10, 10)).save(src)  # 9:16, like output
    frame = scroll_video._build_end_screen_frame(str(src), 1080, 1920)
    assert frame.size == (1080, 1920)
    assert frame.getpixel((0, 0)) != scroll_video.SCROLL_END_SCREEN_BG
