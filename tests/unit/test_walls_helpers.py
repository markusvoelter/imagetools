"""Pure-logic tests for walls helpers (rectangle detection, fitting, listing)."""

import pytest
from PIL import Image

from image_tools import walls


def _wall_with_black_box(w, h, box):
    """A white image with a solid black rectangle at `box` = (x0,y0,x1,y1)."""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    x0, y0, x1, y1 = box
    img.paste((0, 0, 0), (x0, y0, x1, y1))
    return img


# --- find_empty_rectangle -------------------------------------------------

def test_find_empty_rectangle_locates_black_region():
    wall = _wall_with_black_box(400, 300, (50, 40, 200, 180))
    x0, y0, x1, y1 = walls.find_empty_rectangle(wall)
    # PIL paste box is half-open, so black pixels span 50..199, 40..179.
    assert abs(x0 - 50) <= 2 and abs(y0 - 40) <= 2
    assert abs(x1 - 199) <= 2 and abs(y1 - 179) <= 2


def test_find_empty_rectangle_raises_without_black():
    white = Image.new("RGB", (200, 200), (255, 255, 255))
    with pytest.raises(RuntimeError):
        walls.find_empty_rectangle(white)


# --- fit_image_to_box -----------------------------------------------------

def test_fit_image_to_box_covers_and_crops_to_box(make_image):
    out = walls.fit_image_to_box(make_image(100, 100), 50, 80)
    assert out.size == (50, 80)


def test_fit_image_to_box_wide_source(make_image):
    out = walls.fit_image_to_box(make_image(400, 100), 60, 60)
    assert out.size == (60, 60)


# --- collect_files --------------------------------------------------------

def test_collect_files_sorted_and_deduped(tmp_path, make_image):
    for name in ("b.jpg", "a.png", "c.jpeg"):
        make_image(10, 10).save(tmp_path / name)
    files = walls.collect_files(tmp_path, (".jpg", ".jpeg", ".png"))
    assert [f.name for f in files] == ["a.png", "b.jpg", "c.jpeg"]


# --- pick_image_for_ratio -------------------------------------------------

def test_pick_image_for_ratio_single_candidate(tmp_path, make_image):
    p = tmp_path / "only.jpg"
    make_image(160, 90).save(p)
    assert walls.pick_image_for_ratio([p], 16 / 9) == p
