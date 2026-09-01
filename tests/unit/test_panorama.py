"""Tests for the shared panorama-strip helper."""

import pytest

from image_tools import _panorama
from image_tools._panorama import build_panorama_strip, natural_sort_key


def test_natural_sort_key_orders_numerically():
    names = ["img10.jpg", "img2.jpg", "img1.jpg"]
    assert sorted(names, key=natural_sort_key) == \
        ["img1.jpg", "img2.jpg", "img10.jpg"]


def test_build_strip_fixed_slide_count_geometry(image_folder, capture_ctx):
    folder = image_folder([(1000, 1000)] * 4)
    strip, n, section_w, working_h, out_w, out_h = build_panorama_strip(
        str(folder), num_slides=3, aspect_ratio="9:16", ctx=capture_ctx)

    assert (out_w, out_h) == (1080, 1920)
    assert working_h == 1920
    assert section_w == 1080
    assert n == 3
    assert strip.size == (3 * 1080, 1920)


def test_build_strip_use_all_images_derives_slide_count(image_folder, capture_ctx):
    folder = image_folder([(1000, 1000)] * 4)
    strip, n, section_w, *_ = build_panorama_strip(
        str(folder), num_slides=None, aspect_ratio="9:16", ctx=capture_ctx)

    assert n >= 1
    # Strip width is always an exact whole number of slides.
    assert strip.width == n * section_w


def test_build_strip_unknown_aspect_raises(image_folder, capture_ctx):
    folder = image_folder([(1000, 1000)])
    with pytest.raises(ValueError, match="aspect ratio"):
        build_panorama_strip(str(folder), num_slides=2, aspect_ratio="7:5",
                             ctx=capture_ctx)


def test_build_strip_empty_folder_raises(tmp_path, capture_ctx):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="No images"):
        build_panorama_strip(str(empty), num_slides=2, aspect_ratio="9:16",
                             ctx=capture_ctx)
