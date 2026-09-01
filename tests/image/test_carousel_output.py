"""Output tests for the carousel tool."""

from pathlib import Path

import piexif
import pytest
from PIL import Image

from image_tools import carousel


def test_carousel_produces_sized_slides_with_exif(tmp_path, image_folder,
                                                  capture_ctx):
    folder = image_folder([(1000, 1000)] * 4)
    out = tmp_path / "swipey"
    carousel.run(folder=str(folder), num_slides=3, aspect_ratio="9:16",
                 output_dir=str(out), seed=0, ctx=capture_ctx)

    slides = sorted(out.glob("slide_*.jpg"))
    assert len(slides) == 3
    for s in slides:
        with Image.open(s) as im:
            assert im.size == (1080, 1920)
        # Capture time is written into EXIF for a natural carousel ordering.
        exif = piexif.load(str(s))
        assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal]


def test_carousel_multiple_sets_creates_subfolders(tmp_path, image_folder,
                                                   capture_ctx):
    folder = image_folder([(1000, 1000)] * 4)
    out = tmp_path / "sets"
    carousel.run(folder=str(folder), num_slides=2, aspect_ratio="9:16",
                 output_dir=str(out), num_sets=2, random_order=True,
                 seed=0, ctx=capture_ctx)
    assert (out / "set_01").is_dir()
    assert (out / "set_02").is_dir()


def test_carousel_missing_folder_raises(tmp_path, capture_ctx):
    with pytest.raises(ValueError, match="Not a directory"):
        carousel.run(folder=str(tmp_path / "nope"), ctx=capture_ctx)


def test_carousel_unknown_aspect_raises(image_folder, capture_ctx):
    folder = image_folder([(1000, 1000)])
    with pytest.raises(ValueError, match="aspect ratio"):
        carousel.run(folder=str(folder), aspect_ratio="7:5", ctx=capture_ctx)
