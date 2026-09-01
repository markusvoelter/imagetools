"""Output tests for the filmstrip tool."""

import pytest
from PIL import Image

from image_tools import filmstrip


def test_filmstrip_output_is_9x16_canvas(tmp_path, image_folder, capture_ctx):
    folder = image_folder([(800, 800)] * 12)
    out = tmp_path / "strip.jpg"
    result = filmstrip.run(folder=str(folder), num_columns=2,
                           output=str(out), seed=0, ctx=capture_ctx)
    assert result == str(out)
    with Image.open(out) as im:
        assert im.size == filmstrip._canvas_size(2)


def test_filmstrip_repetitions_indexes_files(tmp_path, image_folder,
                                             capture_ctx):
    folder = image_folder([(800, 800)] * 12)
    out = tmp_path / "strip.jpg"
    filmstrip.run(folder=str(folder), num_columns=2, repetitions=2,
                  allow_repeat=True, output=str(out), seed=0, ctx=capture_ctx)
    assert (tmp_path / "strip_01.jpg").exists()
    assert (tmp_path / "strip_02.jpg").exists()


def test_filmstrip_rejects_bad_columns(image_folder, capture_ctx):
    folder = image_folder([(800, 800)] * 4)
    with pytest.raises(ValueError, match="columns"):
        filmstrip.run(folder=str(folder), num_columns=0, ctx=capture_ctx)


def test_filmstrip_rejects_bad_repetitions(image_folder, capture_ctx):
    folder = image_folder([(800, 800)] * 4)
    with pytest.raises(ValueError, match="Repetitions"):
        filmstrip.run(folder=str(folder), repetitions=0, ctx=capture_ctx)


def test_filmstrip_empty_folder_raises(tmp_path, capture_ctx):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="No images"):
        filmstrip.run(folder=str(empty), ctx=capture_ctx)
