"""Pure-logic tests for mosaic image scoring/selection and output naming."""

import random
import re

import pytest

from image_tools import collage


def _img(aspect):
    return {"aspect": aspect, "w": int(round(aspect * 1000)), "h": 1000,
            "path": f"a{aspect}.jpg"}


# --- crop_cost ------------------------------------------------------------

def test_crop_cost_zero_when_aspects_match():
    assert collage.crop_cost(1.5, 1.5) == 0.0


def test_crop_cost_symmetric_and_bounded():
    # 2:1 image in a 1:1 cell wastes half -> cost 0.5, either orientation.
    assert collage.crop_cost(2.0, 1.0) == pytest.approx(0.5)
    assert collage.crop_cost(1.0, 2.0) == pytest.approx(0.5)


# --- select_best_images ---------------------------------------------------

def test_select_best_images_returns_all_when_count_none():
    images = [_img(1.0), _img(1.5)]
    assert collage.select_best_images(images, None, collage.MOSAIC_TILE_SIZES) \
        == images


def test_select_best_images_returns_all_when_count_exceeds_len():
    images = [_img(1.0), _img(1.5)]
    result = collage.select_best_images(images, 5, collage.MOSAIC_TILE_SIZES)
    assert len(result) == len(images)


def test_select_best_images_selects_requested_count_from_input():
    images = [_img(a) for a in (0.4, 0.7, 1.0, 1.5, 2.0, 3.0)]
    random.seed(1234)  # jittered scoring uses the global RNG
    result = collage.select_best_images(images, 3, collage.MOSAIC_TILE_SIZES)
    assert len(result) == 3
    for item in result:
        assert item in images


# --- auto_output_filename -------------------------------------------------

def test_auto_output_filename_columns_includes_col_count():
    name = collage.auto_output_filename("/tmp/myfolder", "columns", 5,
                                        num_cols=2)
    assert re.fullmatch(r"myfolder_columns_5imgs_2cols_\d{8}_\d{6}\.jpg", name)


def test_auto_output_filename_mosaic_includes_aspect_and_selection():
    name = collage.auto_output_filename("/tmp/f", "mosaic", 5, aspect="16:9",
                                        count=8)
    assert re.fullmatch(r"f_mosaic_5imgs_16x9_8sel_\d{8}_\d{6}\.jpg", name)


def test_auto_output_filename_appends_run_sequence():
    name = collage.auto_output_filename("/tmp/f", "grid", 3, seq=2)
    assert re.fullmatch(r"f_grid_3imgs_\d{8}_\d{6}_run2\.jpg", name)
