"""Output tests for the split tool.

split has no randomness, so its output is fully deterministic and a good
early image-producer target. We assert on slide counts, dimensions and error
paths -- never pixel content.
"""

from pathlib import Path

import pytest
from PIL import Image

from image_tools import split


def _slides(root):
    """All produced slides, sorted, from the per-image split subfolder(s)."""
    return sorted(Path(root).rglob("slide_*.jpg"))


def test_wide_image_splits_into_expected_tiles_plus_overview(
        tmp_path, image_folder, capture_ctx):
    # 3000x400 source, 9:16 slide -> piece_w = round(400 * 9/16) = 225.
    # ceil(3000/225) = 14 tiles, + 1 overview slide = 15.
    folder = image_folder([(3000, 400)])
    out = tmp_path / "out"
    split.run(folder=str(folder), aspect_ratio="9:16",
              output_dir=str(out), ctx=capture_ctx)

    slides = _slides(out)
    assert len(slides) == 15
    for s in slides:
        with Image.open(s) as im:
            assert im.size == (225, 400)


def test_source_narrower_than_slide_produces_single_padded_slide(
        tmp_path, image_folder, capture_ctx):
    # 100x400 source is narrower than one 225-wide slide: 1 padded slide,
    # and no overview slide (nothing was actually split).
    folder = image_folder([(100, 400)])
    out = tmp_path / "out"
    split.run(folder=str(folder), aspect_ratio="9:16",
              output_dir=str(out), ctx=capture_ctx)

    slides = _slides(out)
    assert len(slides) == 1
    with Image.open(slides[0]) as im:
        assert im.size == (225, 400)


def test_exact_multiple_has_no_padding_note(tmp_path, image_folder, capture_ctx):
    # 450 = 2 * 225 exactly -> 2 tiles + overview, no padding.
    folder = image_folder([(450, 400)])
    out = tmp_path / "out"
    split.run(folder=str(folder), aspect_ratio="9:16",
              output_dir=str(out), ctx=capture_ctx)
    assert len(_slides(out)) == 3
    assert not any("padded" in line for line in capture_ctx.logs)


def test_unknown_aspect_ratio_raises(tmp_path, image_folder, capture_ctx):
    folder = image_folder([(3000, 400)])
    with pytest.raises(ValueError, match="aspect ratio"):
        split.run(folder=str(folder), aspect_ratio="7:5", ctx=capture_ctx)


def test_empty_folder_raises(tmp_path, capture_ctx):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No images"):
        split.run(folder=str(empty), ctx=capture_ctx)


def test_missing_folder_raises(tmp_path, capture_ctx):
    with pytest.raises(ValueError, match="Not a folder"):
        split.run(folder=str(tmp_path / "nope"), ctx=capture_ctx)
