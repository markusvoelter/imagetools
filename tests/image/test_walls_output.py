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


def test_walls_matches_image_to_least_cropping_wall(tmp_path, capture_ctx):
    # Two walls with very different box shapes: a wide (landscape) hole and a
    # tall (portrait) hole.
    walls_dir = tmp_path / "walls"
    walls_dir.mkdir()
    wide = Image.new("RGB", (800, 800), (255, 255, 255))
    wide.paste((0, 0, 0), (100, 300, 700, 500))  # 600x200 box -> ~3.0
    wide.save(walls_dir / "wide.png")
    tall = Image.new("RGB", (800, 800), (255, 255, 255))
    tall.paste((0, 0, 0), (300, 100, 500, 700))  # 200x600 box -> ~0.33
    tall.save(walls_dir / "tall.png")

    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (1600, 400), (10, 20, 30)).save(photos / "landscape.jpg")
    Image.new("RGB", (400, 1600), (30, 20, 10)).save(photos / "portrait.jpg")

    out = tmp_path / "out"
    walls.run(wall_folder=str(walls_dir), image_folder=str(photos),
              num_outputs=2, output_dir=str(out), seed=0, ctx=capture_ctx)

    logs = capture_ctx.logs
    assert any("wide.png" in l and "landscape.jpg" in l for l in logs)
    assert any("tall.png" in l and "portrait.jpg" in l for l in logs)


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
