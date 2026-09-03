"""Tests for the Tkinter UI layer.

Tk is created in a hidden, headless root (the CI/dev boxes have a display).
The pure persistence layer (`_PersistentStore`) needs no Tk at all. For the
widget-bearing code we instantiate the real tabs and drive their
`_gather_kwargs` / run machinery with the tool `run_fn`s stubbed out, so no
image or video is ever produced.
"""

import json
import os

import pytest

from image_tools import ui


# --------------------------------------------------------------------------
#  _PersistentStore — pure, no Tk
# --------------------------------------------------------------------------

def test_store_fresh_file_creates_default(tmp_path):
    store = ui._PersistentStore(tmp_path / "s.json")
    assert store.list_projects() == [ui._DEFAULT_PROJECT]
    assert store.current_project == ui._DEFAULT_PROJECT


def test_store_get_set_roundtrip_persists(tmp_path):
    path = tmp_path / "s.json"
    store = ui._PersistentStore(path)
    store.set("k", "v")
    store.set("k", "v")  # unchanged -> no-op branch
    # A brand-new store reading the same file sees the value.
    reloaded = ui._PersistentStore(path)
    assert reloaded.get("k", None) == "v"
    assert reloaded.get("missing", "dflt") == "dflt"


def test_store_migrates_legacy_flat_schema(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"some.key": 3, "other": "x"}))
    store = ui._PersistentStore(path)
    assert store.list_projects() == [ui._DEFAULT_PROJECT]
    assert store.get("some.key", None) == 3


def test_store_handles_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ not json")
    store = ui._PersistentStore(path)
    assert store.list_projects() == [ui._DEFAULT_PROJECT]


def test_store_current_project_fallback_when_invalid(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(
        {"current_project": "ghost", "projects": {"A": {}, "B": {}}}))
    store = ui._PersistentStore(path)
    # "ghost" isn't a real project -> falls back to first sorted.
    assert store.current_project == "A"


def test_store_clone_switch_delete(tmp_path):
    events = []
    store = ui._PersistentStore(tmp_path / "s.json")
    store.add_listener(lambda: events.append("changed"))
    store.set("a", 1)

    store.clone_project("Copy")
    assert store.current_project == "Copy"
    assert store.get("a", None) == 1  # data copied over

    store.switch_project(ui._DEFAULT_PROJECT)
    assert store.current_project == ui._DEFAULT_PROJECT
    store.switch_project("nope")  # unknown -> no-op
    store.switch_project(ui._DEFAULT_PROJECT)  # same -> no-op

    store.delete_project("Copy")
    assert "Copy" not in store.list_projects()
    assert events  # listeners fired


def test_store_clone_validation(tmp_path):
    store = ui._PersistentStore(tmp_path / "s.json")
    with pytest.raises(ValueError):
        store.clone_project("   ")
    store.clone_project("Dup")
    store.switch_project(ui._DEFAULT_PROJECT)
    with pytest.raises(ValueError):
        store.clone_project("Dup")


def test_store_delete_last_recreates_default(tmp_path):
    store = ui._PersistentStore(tmp_path / "s.json")
    store.clone_project("Only")
    store.delete_project(ui._DEFAULT_PROJECT)
    store.delete_project("Only")  # deleting the last -> Default recreated
    assert store.list_projects() == [ui._DEFAULT_PROJECT]


def test_store_delete_unknown_is_noop(tmp_path):
    store = ui._PersistentStore(tmp_path / "s.json")
    store.delete_project("ghost")
    assert store.list_projects() == [ui._DEFAULT_PROJECT]


def test_store_listener_exception_isolated(tmp_path):
    store = ui._PersistentStore(tmp_path / "s.json")
    store.add_listener(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    hits = []
    store.add_listener(lambda: hits.append(1))
    store.clone_project("X")  # must not propagate the first listener's error
    assert hits == [1]


def test_store_persist_ignores_oserror(tmp_path, monkeypatch):
    store = ui._PersistentStore(tmp_path / "s.json")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    store.set("k", "v")  # swallowed, no raise


# --------------------------------------------------------------------------
#  Module-level helpers
# --------------------------------------------------------------------------

def test_coerce_stored_variants():
    import tkinter as tk
    assert ui._coerce_stored(tk.StringVar, "abc", "") == "abc"
    assert ui._coerce_stored(tk.StringVar, 12, "") == "12"
    assert ui._coerce_stored(tk.StringVar, None, "") == ""
    assert ui._coerce_stored(tk.BooleanVar, 1, False) is True
    assert ui._coerce_stored(tk.BooleanVar, True, False) is True
    assert ui._coerce_stored(tk.IntVar, None, 7) == 7
    assert ui._coerce_stored(tk.IntVar, 3, 7) == 3


def test_reveal_output_darwin_file(monkeypatch):
    calls = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(ui.sys, "platform", "darwin")
    ui.reveal_output(None)  # empty -> early return
    assert calls == []
    ui.reveal_output("/some/file.mp4")
    assert calls[-1] == ["open", "-R", "/some/file.mp4"]


def test_reveal_output_darwin_dir(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(ui.sys, "platform", "darwin")
    ui.reveal_output(str(tmp_path))
    assert calls[-1] == ["open", str(tmp_path)]


def test_reveal_output_linux(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(ui.sys, "platform", "linux")
    f = tmp_path / "x.mp4"
    f.write_text("x")
    ui.reveal_output(str(f))
    assert calls[-1] == ["xdg-open", str(tmp_path)]


# --------------------------------------------------------------------------
#  Widget-bearing tests: one hidden root shared across the module
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def root():
    import tkinter as tk
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available for Tk")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Give each test its own store + reset the cached global folder var."""
    store = ui._PersistentStore(tmp_path / "ui_state.json")
    monkeypatch.setattr(ui, "_store", store)
    monkeypatch.setattr(ui, "_global_folder_var", None)
    monkeypatch.setattr(ui, "_global_vars", {})
    return store


ALL_TAB_CLASSES = [
    ui.CollageTab, ui.FilmstripTab, ui.RotateVideoTab, ui.CarouselTab,
    ui.ScrollVideoTab, ui.ShuffleRevealTab, ui.ReelTab, ui.KenBurnsTab,
    ui.WallsTab, ui.SplitTab,
]


@pytest.mark.parametrize("tab_cls", ALL_TAB_CLASSES,
                         ids=[c.__name__ for c in ALL_TAB_CLASSES])
def test_tab_builds_and_gathers(tab_cls, root, fresh_store, tmp_path,
                                monkeypatch):
    """Every tab must build its form, and _gather_kwargs must either return a
    kwargs dict or raise a user-facing ValueError (never anything else)."""
    tab = tab_cls(root)

    # No image folder selected yet -> most tabs raise a friendly ValueError.
    monkeypatch.setattr(ui, "_current_image_folder", lambda: "")
    try:
        tab._gather_kwargs()
    except ValueError:
        pass

    # Now with a real folder selected.
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    try:
        kw = tab._gather_kwargs()
        assert isinstance(kw, dict)
    except ValueError:
        pass


def test_gather_kwargs_success_collage(root, fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    tab = ui.CollageTab(root)
    tab.repetitions.set("3")
    tab.num_cols.set("4")
    tab.count.set("12")
    tab.bg.set("#101010")
    kw = tab._gather_kwargs()
    assert kw["folder"] == str(tmp_path)
    assert kw["style"] == "mosaic"
    assert kw["num_cols"] == 4
    assert tab._reps == 3


def test_gather_kwargs_collage_validation(root, fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    tab = ui.CollageTab(root)
    tab.repetitions.set("")
    with pytest.raises(ValueError):
        tab._gather_kwargs()
    tab.repetitions.set("abc")
    with pytest.raises(ValueError):
        tab._gather_kwargs()
    tab.repetitions.set("0")
    with pytest.raises(ValueError):
        tab._gather_kwargs()
    tab.repetitions.set("2")
    tab.num_cols.set("notint")
    with pytest.raises(ValueError):
        tab._gather_kwargs()


def test_gather_kwargs_success_kenburns(root, fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    tab = ui.KenBurnsTab(root)
    kw = tab._gather_kwargs()
    assert kw["folder"] == str(tmp_path)
    assert kw["aspect"] in ("16:9", "9:16")


# --------------------------------------------------------------------------
#  Run / stop / log machinery
# --------------------------------------------------------------------------

def test_run_completes_and_reveals(root, fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    tab = ui.CarouselTab(root)

    produced = tmp_path / "out.mp4"

    def fake_run(**kwargs):
        kwargs["ctx"].log("working")
        return str(produced)

    tab.run_fn = fake_run
    tab._on_run()
    # Worker runs in a background thread; wait for it, then drain the queue.
    if tab.worker:
        tab.worker.join(timeout=5)
    tab._drain_log()
    assert tab.last_output == str(produced)
    assert str(tab.status.cget("text")) in ("Done", "Running...")

    # _open_output should call reveal_output with the produced path.
    revealed = []
    monkeypatch.setattr(ui, "reveal_output", lambda p: revealed.append(p))
    tab._open_output()
    assert revealed == [str(produced)]

    tab._clear_log()


def test_run_reports_error(root, fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    tab = ui.CarouselTab(root)

    def boom(**kwargs):
        raise RuntimeError("kaboom")

    tab.run_fn = boom
    tab._on_run()
    if tab.worker:
        tab.worker.join(timeout=5)
    tab._drain_log()
    log_text = tab.log.get("1.0", "end")
    assert "kaboom" in log_text


def test_on_run_gather_error_logs(root, fresh_store, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: "")
    tab = ui.CollageTab(root)
    tab._on_run()  # gather raises ValueError -> logged, no worker started
    assert tab.worker is None
    assert "[error]" in tab.log.get("1.0", "end")


def test_on_stop_cancels(root, fresh_store, monkeypatch):
    tab = ui.CarouselTab(root)
    tab._on_stop()  # no ctx -> no-op
    from image_tools import RunContext
    tab.ctx = RunContext()
    tab._on_stop()
    assert tab.ctx.cancelled()


# --------------------------------------------------------------------------
#  Default outputs land in the input folder, named "<project> <tool> <ts>"
# --------------------------------------------------------------------------

import re


def _assert_default_output(path, folder, project, label, ext):
    # Output lands in a per-tool subdirectory named by the tab label.
    assert os.path.dirname(path) == os.path.join(str(folder), label)
    pat = rf"{re.escape(project)} {re.escape(label)} \d{{8}}_\d{{6}}{re.escape(ext)}"
    assert re.fullmatch(pat, os.path.basename(path)), path


def test_default_output_helper_names_by_project_tool_and_timestamp(
        root, fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    fresh_store.clone_project("Holiday 2026")
    tab = ui.ShuffleRevealTab(root)
    _assert_default_output(tab._default_output(str(tmp_path)),
                           tmp_path, "Holiday 2026", "fast scroll", ".mp4")


def test_shuffle_output_defaults_into_input_folder(root, fresh_store, tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    fresh_store.clone_project("Holiday 2026")
    tab = ui.ShuffleRevealTab(root)
    _assert_default_output(tab._gather_kwargs()["output"],
                           tmp_path, "Holiday 2026", "fast scroll", ".mp4")


def test_collage_output_defaults_into_input_folder(root, fresh_store, tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    fresh_store.clone_project("Trip")
    tab = ui.CollageTab(root)
    tab.style.set("grid")
    tab.repetitions.set("1")
    _assert_default_output(tab._gather_kwargs()["output"],
                           tmp_path, "Trip", "collage", ".jpg")


def test_split_output_dir_defaults_into_input_folder(root, fresh_store,
                                                     tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    fresh_store.clone_project("Trip")
    tab = ui.SplitTab(root)
    # Directory output: same naming, no extension.
    _assert_default_output(tab._gather_kwargs()["output_dir"],
                           tmp_path, "Trip", "split", "")


def test_output_respects_explicit_override(root, fresh_store, tmp_path,
                                           monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    tab = ui.ShuffleRevealTab(root)
    tab.output.set("/somewhere/else.mp4")
    assert tab._gather_kwargs()["output"] == "/somewhere/else.mp4"


# --------------------------------------------------------------------------
#  Shared project-bar fields (music, title, subtitle, fonts) feed the tabs
# --------------------------------------------------------------------------

def test_global_music_flows_into_music_tabs(root, fresh_store, tmp_path,
                                            monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    ui._make_global_var(ui._GLOBAL_MUSIC_KEY, ui._DEFAULT_MUSIC).set(
        "/music/song.mp3")
    for tab_cls in (ui.ReelTab, ui.ScrollVideoTab, ui.ShuffleRevealTab,
                    ui.KenBurnsTab):
        assert tab_cls(root)._gather_kwargs()["music"] == "/music/song.mp3"


def test_global_music_empty_is_omitted(root, fresh_store, tmp_path,
                                       monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    ui._make_global_var(ui._GLOBAL_MUSIC_KEY, ui._DEFAULT_MUSIC).set("")
    assert "music" not in ui.ReelTab(root)._gather_kwargs()


def test_global_title_subtitle_and_fonts_flow_into_kenburns(
        root, fresh_store, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    ui._make_global_var(ui._GLOBAL_TITLE_KEY).set("My Title")
    ui._make_global_var(ui._GLOBAL_SUBTITLE_KEY).set("My Subtitle")
    ui._make_global_var(ui._GLOBAL_TITLE_FONT_KEY).set("/fonts/a.ttf")
    ui._make_global_var(ui._GLOBAL_SUBTITLE_FONT_KEY).set("/fonts/b.ttf")

    kw = ui.KenBurnsTab(root)._gather_kwargs()

    assert kw["title"] == "My Title"
    assert kw["subtitle"] == "My Subtitle"
    assert kw["title_font"] == "/fonts/a.ttf"
    assert kw["subtitle_font"] == "/fonts/b.ttf"


def test_template_set_selects_title_and_end_screens(root, fresh_store,
                                                    tmp_path, monkeypatch):
    import image_tools
    templates = tmp_path / "templates"
    (templates / "fancy" / "16_9").mkdir(parents=True)
    for kind in ("title-screen", "end-screen"):
        (templates / "fancy" / "16_9" / f"{kind}.jpg").write_bytes(b"x")
    monkeypatch.setattr(image_tools, "TEMPLATES_DIR", str(templates))
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    ui._make_global_var(ui._GLOBAL_TEMPLATE_SET_KEY, "").set("fancy")

    tab = ui.KenBurnsTab(root)
    tab.aspect.set("16:9")
    kw = tab._gather_kwargs()

    assert kw["title_screen"] == str(
        templates / "fancy" / "16_9" / "title-screen.jpg")
    assert kw["end_screen"] == str(
        templates / "fancy" / "16_9" / "end-screen.jpg")


def test_rotate_tab_uses_template_end_screen(root, fresh_store, tmp_path,
                                             monkeypatch):
    import image_tools
    templates = tmp_path / "templates"
    (templates / "mvp" / "9_16").mkdir(parents=True)
    end = templates / "mvp" / "9_16" / "end-screen.jpg"
    end.write_bytes(b"x")
    monkeypatch.setattr(image_tools, "TEMPLATES_DIR", str(templates))
    monkeypatch.setattr(ui, "_current_image_folder", lambda: str(tmp_path))
    ui._make_global_var(ui._GLOBAL_TEMPLATE_SET_KEY, "").set("mvp")

    tab = ui.RotateVideoTab(root)
    tab.duration.set("10")
    tab.cover.set("cover.jpg")

    assert tab._gather_kwargs()["end_screen"] == str(end)


# --------------------------------------------------------------------------
#  Changing the images folder forces creation of a new, named project
# --------------------------------------------------------------------------

def test_change_images_folder_creates_named_project(root, fresh_store, tmp_path,
                                                    monkeypatch):
    target = tmp_path / "shoot"
    target.mkdir()
    monkeypatch.setattr(ui.filedialog, "askdirectory", lambda **k: str(target))
    monkeypatch.setattr(ui.simpledialog, "askstring", lambda *a, **k: "My Shoot")

    name = ui._change_images_folder(root)

    assert name == "My Shoot"
    assert fresh_store.current_project == "My Shoot"
    assert "My Shoot" in fresh_store.list_projects()
    assert ui._current_image_folder() == str(target)


def test_change_images_folder_cancel_directory_is_noop(root, fresh_store,
                                                       monkeypatch):
    before = list(fresh_store.list_projects())
    monkeypatch.setattr(ui.filedialog, "askdirectory", lambda **k: "")
    monkeypatch.setattr(ui.simpledialog, "askstring",
                        lambda *a, **k: pytest.fail("must not prompt for a name"))

    assert ui._change_images_folder(root) is None
    assert fresh_store.list_projects() == before
    assert ui._current_image_folder() == ""


def test_change_images_folder_cancel_name_leaves_project_unchanged(
        root, fresh_store, tmp_path, monkeypatch):
    target = tmp_path / "shoot"
    target.mkdir()
    before = list(fresh_store.list_projects())
    monkeypatch.setattr(ui.filedialog, "askdirectory", lambda **k: str(target))
    monkeypatch.setattr(ui.simpledialog, "askstring", lambda *a, **k: None)

    assert ui._change_images_folder(root) is None
    assert fresh_store.list_projects() == before
    assert fresh_store.current_project == before[0]
    assert ui._current_image_folder() == ""


# --------------------------------------------------------------------------
#  Background image-folder watcher
# --------------------------------------------------------------------------

def test_folder_status_empty():
    assert ui._folder_status("") == ("empty", "")


def test_folder_status_missing(tmp_path):
    state, detail = ui._folder_status(str(tmp_path / "gone"))
    assert state == "missing"


def test_folder_status_no_images(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    state, detail = ui._folder_status(str(tmp_path))
    assert state == "no_images"


def test_folder_status_ok_counts_images(image_folder):
    folder = image_folder([(10, 10), (10, 10), (10, 10)])
    (folder / "readme.txt").write_text("ignore me")
    state, count = ui._folder_status(str(folder))
    assert state == "ok"
    assert count == 3


def test_folder_status_various_extensions(tmp_path, make_image):
    make_image(10, 10).save(tmp_path / "a.png")
    make_image(10, 10).save(tmp_path / "b.webp")
    state, count = ui._folder_status(str(tmp_path))
    assert state == "ok"
    assert count == 2


def _make_watcher(root, folder_var, interval_ms=10_000):
    import tkinter as tk
    label = ui.ttk.Label(root)
    watcher = ui._FolderWatcher(root, folder_var, label, interval_ms=interval_ms)
    return watcher, label


def test_watcher_initial_ok_state(root, image_folder):
    import tkinter as tk
    folder = image_folder([(10, 10)])
    var = tk.StringVar(value=str(folder))
    watcher, label = _make_watcher(root, var)
    try:
        assert "✓" in label.cget("text")
        assert str(label.cget("foreground")) == "#4caf50"
    finally:
        watcher.stop()


def test_watcher_flags_missing_folder(root, tmp_path):
    import tkinter as tk
    var = tk.StringVar(value=str(tmp_path / "nope"))
    watcher, label = _make_watcher(root, var)
    try:
        assert "⚠" in label.cget("text")
        assert str(label.cget("foreground")) == "#e05252"
    finally:
        watcher.stop()


def test_watcher_flags_empty_folder(root, tmp_path):
    import tkinter as tk
    var = tk.StringVar(value=str(tmp_path))
    watcher, label = _make_watcher(root, var)
    try:
        assert "⚠" in label.cget("text")
        assert "no images" in label.cget("text")
    finally:
        watcher.stop()


def test_watcher_blank_selection_clears_label(root):
    import tkinter as tk
    var = tk.StringVar(value="")
    watcher, label = _make_watcher(root, var)
    try:
        assert label.cget("text") == ""
    finally:
        watcher.stop()


def test_watcher_drain_applies_queued_status(root, tmp_path):
    """The Tk-thread drain renders whatever the worker most recently posted."""
    import tkinter as tk
    var = tk.StringVar(value=str(tmp_path))
    watcher, label = _make_watcher(root, var)
    try:
        # Simulate the worker posting a fresh 'missing' result, then draining.
        watcher._queue.put(("missing", "folder not found"))
        watcher._drain()
        assert "folder not found" in label.cget("text")
    finally:
        watcher.stop()


def test_watcher_reacts_to_folder_change(root, image_folder, tmp_path):
    """Changing the folder var re-checks and updates the flag within ~2s."""
    import time
    import tkinter as tk
    good = image_folder([(10, 10)])
    var = tk.StringVar(value=str(good))
    watcher, label = _make_watcher(root, var, interval_ms=10_000)
    try:
        assert "✓" in label.cget("text")
        # Point at a non-existent folder; the write-trace wakes the worker.
        var.set(str(tmp_path / "vanished"))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            root.update()  # process pending 'after' drains
            if "⚠" in label.cget("text"):
                break
            time.sleep(0.02)
        assert "⚠" in label.cget("text")
    finally:
        watcher.stop()


def test_watcher_stop_is_idempotent(root, tmp_path):
    import tkinter as tk
    var = tk.StringVar(value=str(tmp_path))
    watcher, _ = _make_watcher(root, var)
    watcher.stop()
    watcher.stop()  # second call must not raise
    assert watcher._stop.is_set()


# --------------------------------------------------------------------------
#  Selected tab is remembered per project
# --------------------------------------------------------------------------

def _notebook(root, *labels):
    from tkinter import ttk
    nb = ttk.Notebook(root)
    for label in labels:
        nb.add(ttk.Frame(nb), text=label)
    return nb


def test_selected_tab_saved_and_restored(root, fresh_store):
    nb = _notebook(root, "Alpha", "Beta", "Gamma")
    nb.select(nb.tabs()[1])  # user picks Beta
    ui._save_selected_tab(nb)
    assert fresh_store.get(ui._GLOBAL_SELECTED_TAB_KEY, "") == "Beta"

    # A freshly built notebook (as on restart) restores the stored tab.
    nb2 = _notebook(root, "Alpha", "Beta", "Gamma")
    ui._restore_selected_tab(nb2)
    assert nb2.tab(nb2.select(), "text") == "Beta"
    nb.destroy()
    nb2.destroy()


def test_selected_tab_is_per_project(root, fresh_store):
    nb = _notebook(root, "Alpha", "Beta")
    fresh_store.clone_project("Other")  # switch to a second project
    nb.select(nb.tabs()[1])
    ui._save_selected_tab(nb)  # Other -> Beta

    fresh_store.switch_project(ui._DEFAULT_PROJECT)
    ui._restore_selected_tab(nb)  # Default has no stored tab -> unchanged
    assert nb.tab(nb.select(), "text") == "Beta"

    fresh_store.switch_project("Other")
    nb.select(nb.tabs()[0])  # move away, then restore Other's choice
    ui._restore_selected_tab(nb)
    assert nb.tab(nb.select(), "text") == "Beta"
    nb.destroy()


def test_restore_selected_tab_ignores_unknown_label(root, fresh_store):
    fresh_store.set(ui._GLOBAL_SELECTED_TAB_KEY, "Nonexistent")
    nb = _notebook(root, "Alpha", "Beta")
    nb.select(nb.tabs()[0])
    ui._restore_selected_tab(nb)  # no matching tab -> no change, no error
    assert nb.tab(nb.select(), "text") == "Alpha"
    nb.destroy()


# --------------------------------------------------------------------------
#  main() — build the whole window, but never actually enter the event loop
# --------------------------------------------------------------------------

def test_main_builds_window(fresh_store, monkeypatch):
    import tkinter as tk
    destroyed = []

    def fake_mainloop(self):
        destroyed.append(self)
        self.destroy()

    monkeypatch.setattr(tk.Misc, "mainloop", fake_mainloop)
    ui.main()
    assert destroyed  # mainloop was reached with a fully built window
