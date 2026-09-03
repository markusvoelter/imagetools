"""Output tests for the walls tool."""

import pytest
from PIL import Image

from image_tools import walls


@pytest.fixture
def wall_folder(tmp_path, make_image):
    """A folder holding one wall scaffold: white with a central black box."""
    folder = tmp_path / "walls"
    folder.mkdir()
    wall = Image.new("RGB", (800, 600), (255, 255, 255))
    wall.paste((0, 0, 0), (150, 120, 650, 480))  # large rectangular hole
    wall.save(folder / "wall.png")
    return folder


@pytest.fixture
def photo_folder(tmp_path, make_image):
    folder = tmp_path / "photos"
    folder.mkdir()
    for i, size in enumerate([(1200, 800), (800, 1200), (1000, 1000)]):
        make_image(*size).save(folder / f"photo_{i}.jpg", quality=90)
    return folder


def test_walls_varies_walls_while_favoring_low_crop(tmp_path, capture_ctx):
    # Two walls with different box shapes: a wide (landscape) hole and a tall
    # (portrait) hole.
    walls_dir = tmp_path / "walls"
    walls_dir.mkdir()
    wide = Image.new("RGB", (800, 800), (255, 255, 255))
    wide.paste((0, 0, 0), (100, 300, 700, 500))  # 600x200 box -> ~3.0
    wide.save(walls_dir / "wide.png")
    tall = Image.new("RGB", (800, 800), (255, 255, 255))
    tall.paste((0, 0, 0), (300, 100, 500, 700))  # 200x600 box -> ~0.33
    tall.save(walls_dir / "tall.png")

    # All-landscape photos: the wide wall crops them least.
    photos = tmp_path / "photos"
    photos.mkdir()
    for i in range(3):
        Image.new("RGB", (1600, 400), (10, 20, 30)).save(photos / f"p{i}.jpg")

    out = tmp_path / "out"
    walls.run(wall_folder=str(walls_dir), image_folder=str(photos),
              num_outputs=24, output_dir=str(out), seed=0, ctx=capture_ctx)

    lines = [l for l in capture_ctx.logs if "composite_" in l and "->" in l]
    used_wide = sum("wide.png" in l for l in lines)
    used_tall = sum("tall.png" in l for l in lines)
    assert used_wide > used_tall     # low-crop wall favored
    assert used_wide > 0 and used_tall > 0  # ...but both walls get used


def test_walls_produces_composites(tmp_path, wall_folder, photo_folder,
                                   capture_ctx):
    out = tmp_path / "out"
    walls.run(wall_folder=str(wall_folder), image_folder=str(photo_folder),
              num_outputs=2, output_dir=str(out), seed=0, ctx=capture_ctx)

    composites = sorted(out.glob("composite_*.png"))
    assert len(composites) == 2
    for c in composites:
        with Image.open(c) as im:
            assert im.size == (800, 600)  # composite keeps the wall's size


def test_walls_bad_wall_folder_raises(photo_folder, tmp_path, capture_ctx):
    with pytest.raises(ValueError, match="Wall folder"):
        walls.run(wall_folder=str(tmp_path / "nope"),
                  image_folder=str(photo_folder), num_outputs=1,
                  ctx=capture_ctx)


def test_walls_bad_image_folder_raises(wall_folder, tmp_path, capture_ctx):
    with pytest.raises(ValueError, match="Image folder"):
        walls.run(wall_folder=str(wall_folder),
                  image_folder=str(tmp_path / "nope"), num_outputs=1,
                  ctx=capture_ctx)


def test_walls_rejects_zero_outputs(wall_folder, photo_folder, capture_ctx):
    with pytest.raises(ValueError, match="num_outputs"):
        walls.run(wall_folder=str(wall_folder),
                  image_folder=str(photo_folder), num_outputs=0,
                  ctx=capture_ctx)


def test_walls_empty_image_folder_raises(wall_folder, tmp_path, capture_ctx):
    empty = tmp_path / "empty_photos"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="No image files"):
        walls.run(wall_folder=str(wall_folder), image_folder=str(empty),
                  num_outputs=1, ctx=capture_ctx)
