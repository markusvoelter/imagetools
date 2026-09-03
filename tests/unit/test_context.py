"""Tests for RunContext (logging, cancellation, process teardown) and
the template_for asset lookup helper."""

import pytest

import image_tools
from image_tools import RunContext, RunCancelled, template_for


# --- RunContext.log -------------------------------------------------------

def test_log_uses_custom_callback():
    seen = []
    ctx = RunContext(log=seen.append)
    ctx.log("hello")
    ctx.log(42)  # coerced to str
    assert seen == ["hello", "42"]


def test_log_defaults_to_print(capsys):
    ctx = RunContext()
    ctx.log("printed")
    assert "printed" in capsys.readouterr().out


# --- cancellation ---------------------------------------------------------

def test_cancelled_flag_and_check():
    ctx = RunContext(log=lambda m: None)
    assert ctx.cancelled() is False
    ctx.check_cancelled()  # no raise
    ctx.cancel()
    assert ctx.cancelled() is True
    with pytest.raises(RunCancelled):
        ctx.check_cancelled()


class _FakeProc:
    def __init__(self, boom=False):
        self.terminated = False
        self._boom = boom

    def terminate(self):
        if self._boom:
            raise OSError("already gone")
        self.terminated = True


def test_cancel_terminates_registered_processes():
    ctx = RunContext(log=lambda m: None)
    p1, p2 = _FakeProc(), _FakeProc()
    ctx.register_process(p1)
    ctx.register_process(p2)
    ctx.cancel()
    assert p1.terminated and p2.terminated


def test_cancel_swallows_terminate_errors():
    ctx = RunContext(log=lambda m: None)
    good = _FakeProc()
    ctx.register_process(_FakeProc(boom=True))
    ctx.register_process(good)
    ctx.cancel()  # must not raise despite the first process failing
    assert good.terminated


# --- template_for ---------------------------------------------------------

def test_template_for_returns_none_without_aspect():
    assert template_for(None, "title-screen") is None


def test_template_for_returns_none_when_missing():
    assert template_for("16:9", "does-not-exist") is None


def test_template_for_finds_existing_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tools, "TEMPLATES_DIR", str(tmp_path))
    folder = tmp_path / "16_9"
    folder.mkdir()
    asset = folder / "title-screen.jpg"
    asset.write_bytes(b"not-really-a-jpeg")
    assert template_for("16:9", "title-screen") == str(asset)


def test_template_for_uses_template_set_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tools, "TEMPLATES_DIR", str(tmp_path))
    folder = tmp_path / "mvp" / "16_9"
    folder.mkdir(parents=True)
    asset = folder / "end-screen.jpg"
    asset.write_bytes(b"x")
    assert template_for("16:9", "end-screen", template_set="mvp") == str(asset)
    # Without the set, the aspect folder isn't found directly under templates.
    assert template_for("16:9", "end-screen") is None


def test_template_for_falls_back_to_png(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tools, "TEMPLATES_DIR", str(tmp_path))
    folder = tmp_path / "16_9"
    folder.mkdir()
    asset = folder / "title-screen.png"  # only a PNG exists
    asset.write_bytes(b"x")
    assert template_for("16:9", "title-screen") == str(asset)


def test_template_for_prefers_jpg_over_png(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tools, "TEMPLATES_DIR", str(tmp_path))
    folder = tmp_path / "16_9"
    folder.mkdir()
    (folder / "end-screen.png").write_bytes(b"x")
    jpg = folder / "end-screen.jpg"
    jpg.write_bytes(b"x")
    assert template_for("16:9", "end-screen") == str(jpg)


def test_list_template_sets_returns_sorted_subdirs(tmp_path, monkeypatch):
    monkeypatch.setattr(image_tools, "TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "mvp").mkdir()
    (tmp_path / "fancy").mkdir()
    (tmp_path / "note.txt").write_text("not a dir")
    assert image_tools.list_template_sets() == ["fancy", "mvp"]
