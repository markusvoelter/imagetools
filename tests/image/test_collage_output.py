"""Output tests for the collage tool (all four styles).

Randomness is pinned via the run(seed=...) parameter so runs are reproducible;
assertions are structural (file validity + dimensions), never pixel content.
"""

import pytest
from PIL import Image

from image_tools import collage


def _assert_valid_jpeg(path, size=None):
    with Image.open(path) as im:
        im.verify()
    with Image.open(path) as im:
        assert im.format == "JPEG"
        if size is not None:
            assert im.size == size


def test_grid_output_is_canvas_width(tmp_path, image_folder, capture_ctx):
    folder = image_folder([(1500, 1000)] * 8)
    out = tmp_path / "grid.jpg"
    result = collage.run(folder=str(folder), style="grid", output=str(out),
                         seed=0, ctx=capture_ctx)
    assert result == str(out)
    with Image.open(out) as im:
        assert im.width == collage.CANVAS_WIDTH


def test_columns_output_valid(tmp_path, image_folder, capture_ctx):
    folder = image_folder([(1500, 1000)] * 8)
    out = tmp_path / "cols.jpg"
    collage.run(folder=str(folder), style="columns", num_cols=2,
                output=str(out), seed=0, ctx=capture_ctx)
    _assert_valid_jpeg(out)


def test_creative_output_valid(tmp_path, image_folder, capture_ctx):
    folder = image_folder([(1500, 1000), (1000, 1500), (1200, 1200)] * 3)
    out = tmp_path / "creative.jpg"
    collage.run(folder=str(folder), style="creative", output=str(out),
                seed=0, ctx=capture_ctx)
    _assert_valid_jpeg(out)


def test_mosaic_output_matches_requested_aspect(tmp_path, image_folder,
                                                capture_ctx):
    folder = image_folder([(1500, 1000), (1000, 1500), (1200, 1200)] * 3)
    out = tmp_path / "mosaic.jpg"
    collage.run(folder=str(folder), style="mosaic", aspect="16:9",
                output=str(out), seed=0, ctx=capture_ctx)
    # Mosaic canvas is CANVAS_WIDTH x CANVAS_WIDTH/aspect.
    _assert_valid_jpeg(out, size=(collage.CANVAS_WIDTH,
                                  int(collage.CANVAS_WIDTH / (16 / 9))))


def test_unknown_style_raises(tmp_path, image_folder, capture_ctx):
    folder = image_folder([(1500, 1000)] * 4)
    with pytest.raises(ValueError, match="Unknown style"):
        collage.run(folder=str(folder), style="spiral", ctx=capture_ctx)


def test_missing_folder_raises(tmp_path, capture_ctx):
    with pytest.raises(ValueError, match="Not a directory"):
        collage.run(folder=str(tmp_path / "nope"), style="grid",
                    ctx=capture_ctx)


def test_empty_folder_raises(tmp_path, capture_ctx):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="No images"):
        collage.run(folder=str(empty), style="grid", ctx=capture_ctx)
