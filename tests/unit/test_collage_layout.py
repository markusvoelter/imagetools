"""Pure-logic tests for collage layout maths (no image I/O)."""

import math

import pytest

from image_tools import collage


def _img(aspect):
    """A minimal image record as consumed by the layout functions."""
    return {"aspect": aspect, "w": int(round(aspect * 1000)), "h": 1000,
            "path": "x.jpg"}


# --- compute_row_height / compute_col_width -------------------------------

def test_compute_row_height_basic():
    # (canvas_w - gap*(n-1)) / sum(aspects)
    assert collage.compute_row_height([1.0, 1.0], 1000, 0) == 500.0
    assert collage.compute_row_height([1.0, 1.0], 1000, 20) == pytest.approx(490.0)


def test_compute_col_width_mirrors_row_height():
    # Columns use inverse aspects but the same arithmetic.
    assert collage.compute_col_width([1.0, 1.0], 1000, 0) == 500.0


# --- row_cost / col_cost --------------------------------------------------

def test_row_cost_zero_at_target():
    assert collage.row_cost(collage.TARGET_ROW_HEIGHT) == 0.0


def test_row_cost_infinite_out_of_bounds():
    assert collage.row_cost(collage.MIN_ROW_HEIGHT - 1) == math.inf
    assert collage.row_cost(collage.MAX_ROW_HEIGHT + 1) == math.inf


def test_row_cost_increases_with_deviation():
    near = collage.row_cost(collage.TARGET_ROW_HEIGHT + 50)
    far = collage.row_cost(collage.TARGET_ROW_HEIGHT + 150)
    assert 0 < near < far


def test_col_cost_zero_at_target():
    assert collage.col_cost(collage.TARGET_COL_WIDTH) == 0.0
    assert collage.col_cost(collage.MAX_COL_WIDTH + 1) == math.inf


# --- dp_layout / dp_column_layout -----------------------------------------

def _assert_partition(rows, n):
    """Rows must tile [0, n) contiguously and in order."""
    assert [i for row in rows for i in row] == list(range(n))
    for row in rows:
        assert row == list(range(row[0], row[-1] + 1))
        assert len(row) >= 1


def test_dp_layout_partitions_all_images_in_order():
    images = [_img(1.5) for _ in range(10)]
    rows = collage.dp_layout(images, collage.CANVAS_WIDTH, collage.GRID_GAP)
    _assert_partition(rows, len(images))


def test_dp_layout_rows_are_within_height_bounds():
    images = [_img(1.5) for _ in range(10)]
    rows = collage.dp_layout(images, collage.CANVAS_WIDTH, collage.GRID_GAP)
    for row in rows:
        aspects = [images[i]["aspect"] for i in row]
        h = collage.compute_row_height(aspects, collage.CANVAS_WIDTH,
                                       collage.GRID_GAP)
        assert collage.MIN_ROW_HEIGHT <= h <= collage.MAX_ROW_HEIGHT


def test_dp_column_layout_partitions_all_images():
    images = [_img(0.66) for _ in range(10)]
    cols = collage.dp_column_layout(images, collage.COLUMN_CANVAS_HEIGHT,
                                    collage.GRID_GAP)
    _assert_partition(cols, len(images))


# --- fixed_column_layout --------------------------------------------------

def test_fixed_column_layout_distributes_remainder_to_leading_columns():
    images = [_img(1.0) for _ in range(10)]
    cols = collage.fixed_column_layout(images, 3, 4000, 15)
    assert [len(c) for c in cols] == [4, 3, 3]
    _assert_partition(cols, 10)


def test_fixed_column_layout_even_split():
    images = [_img(1.0) for _ in range(9)]
    cols = collage.fixed_column_layout(images, 3, 4000, 15)
    assert [len(c) for c in cols] == [3, 3, 3]


# --- compute_column_canvas_height -----------------------------------------

def test_compute_column_canvas_height_formula():
    images = [_img(1.0) for _ in range(12)]
    # target_w = 4000 - 15*(3-1) = 3970; h = 3970 * 12 / 3**2 = 5293.33 -> 5293
    assert collage.compute_column_canvas_height(images, 3) == 5293


def test_compute_column_canvas_height_has_floor():
    images = [_img(4.0) for _ in range(2)]  # very wide -> tiny computed height
    assert collage.compute_column_canvas_height(images, 4) == 1000


# --- interleave_by_aspect -------------------------------------------------

def test_interleave_by_aspect_three_landscapes_then_one_portrait():
    images = [_img(a) for a in (0.5, 0.6, 1.5, 1.6, 1.7, 1.8)]
    order = collage.interleave_by_aspect(images)
    assert [images[i]["aspect"] for i in order] == [1.5, 1.6, 1.7, 0.5, 1.8, 0.6]
