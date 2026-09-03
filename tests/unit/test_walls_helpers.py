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


# --- crop_cost ------------------------------------------------------------

def test_crop_cost_zero_when_aspects_match():
    assert walls.crop_cost(1.5, 1.5) == 0.0


def test_crop_cost_grows_with_mismatch_and_is_symmetric():
    near = walls.crop_cost(1.6, 1.5)
    far = walls.crop_cost(2.4, 1.5)
    assert 0 < near < far
    assert walls.crop_cost(2.0, 1.0) == pytest.approx(walls.crop_cost(1.0, 2.0))


# --- choose_wall ----------------------------------------------------------

def test_choose_wall_favors_low_crop_but_still_varies():
    import random
    from collections import Counter

    candidates = [("wide", 3.0), ("tall", 0.33)]
    random.seed(0)
    picks = Counter(walls.choose_wall(candidates, 4.0)[0] for _ in range(200))
    # A 4:1 image crops far less in the wide box, so it's chosen more often...
    assert picks["wide"] > picks["tall"]
    # ...but the tall wall still shows up: the random element is preserved.
    assert picks["tall"] > 0


# --- wall_box_ratio -------------------------------------------------------

def test_wall_box_ratio_reflects_empty_rectangle(tmp_path, make_image):
    # A 200-wide by 100-tall black box -> ~2.0 aspect ratio.
    wall = _wall_with_black_box(400, 400, (50, 100, 250, 200))
    path = tmp_path / "wall.png"
    wall.save(path)
    assert walls.wall_box_ratio(path) == pytest.approx(2.0, abs=0.05)
