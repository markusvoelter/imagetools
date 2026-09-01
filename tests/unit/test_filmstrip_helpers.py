"""Pure-logic tests for filmstrip helpers (geometry + colour parsing)."""

import pytest

from image_tools import filmstrip


# --- _canvas_size ---------------------------------------------------------

def test_canvas_size_is_locked_to_9x16():
    w, h = filmstrip._canvas_size(2)
    # STRIP_W = 700 + 2*66 = 832; w = 2*832 + 44 + 140 = 1848.
    assert w == 1848
    assert h == round(w * 16 / 9)


def test_canvas_size_grows_with_columns():
    w1, _ = filmstrip._canvas_size(1)
    w3, _ = filmstrip._canvas_size(3)
    assert w3 > w1


# --- _parse_bg ------------------------------------------------------------

def test_parse_bg_default_when_empty():
    assert filmstrip._parse_bg(None) == filmstrip.DEFAULT_BG
    assert filmstrip._parse_bg("") == filmstrip.DEFAULT_BG


def test_parse_bg_hex_with_and_without_hash():
    assert filmstrip._parse_bg("#ff0000") == (255, 0, 0)
    assert filmstrip._parse_bg("00ff00") == (0, 255, 0)


def test_parse_bg_accepts_tuple():
    assert filmstrip._parse_bg((10, 20, 30)) == (10, 20, 30)


@pytest.mark.parametrize("bad", ["#12345", "xyz", "gggggg"])
def test_parse_bg_rejects_invalid(bad):
    with pytest.raises(ValueError):
        filmstrip._parse_bg(bad)


# --- _fit_width -----------------------------------------------------------

def test_fit_width_preserves_aspect(make_image):
    out = filmstrip._fit_width(make_image(400, 200), 700)
    assert out.size == (700, 350)


# --- _peek_height ---------------------------------------------------------

def test_peek_height_crop_returns_fixed_frame_height(image_folder):
    folder = image_folder([(400, 200)])
    path = str(next(folder.iterdir()))
    assert filmstrip._peek_height(path, crop=True) == filmstrip.PHOTO_H


def test_peek_height_no_crop_follows_image_aspect(image_folder):
    folder = image_folder([(700, 350)])
    path = str(next(folder.iterdir()))
    # width-fit to PHOTO_W keeps aspect: 700x350 -> 700 wide -> 350 tall.
    assert filmstrip._peek_height(path, crop=False) == 350
