"""Tkinter UI for the image-tools package."""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

from . import (
    OUTPUT_DIR,
    PROJECT_ROOT,
    RunCancelled,
    RunContext,
    WALLS_DIR,
    ensure_output_dir,
    list_template_sets,
    template_for,
)
from . import carousel as carousel_mod
from . import collage as collage_mod
from . import filmstrip as filmstrip_mod
from . import ken_burns as ken_burns_mod
from . import reel as reel_mod
from . import rotate_video as rotate_video_mod
from . import scroll_video as scroll_video_mod
from . import shuffle_reveal as shuffle_reveal_mod
from . import split as split_mod
from . import walls as walls_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pick_dir(var, initial=None):
    path = filedialog.askdirectory(
        initialdir=var.get() or initial or PROJECT_ROOT
    )
    if path:
        var.set(path)


def pick_color(var):
    initial = var.get().strip() or "#333333"
    if not initial.startswith("#"):
        initial = "#" + initial
    try:
        result = colorchooser.askcolor(color=initial, title="Pick a color")
    except tk.TclError:
        result = colorchooser.askcolor(title="Pick a color")
    if result and result[1]:
        var.set(result[1])


def reveal_output(path):
    """Show `path` in the OS file manager.

    Files are revealed with the file selected (Finder's "Show in Finder").
    Folders are opened directly.
    """
    if not path:
        return
    if sys.platform == "darwin":
        if os.path.isdir(path):
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["open", "-R", path])
    elif sys.platform.startswith("linux"):
        target = path if os.path.isdir(path) else os.path.dirname(path)
        subprocess.Popen(["xdg-open", target])
    elif sys.platform == "win32":
        if os.path.isdir(path):
            os.startfile(path)
        else:
            subprocess.Popen(["explorer", "/select,", path])


# ---------------------------------------------------------------------------
# Persistent field state
# ---------------------------------------------------------------------------

_STATE_PATH = Path.cwd() / ".image_tools_ui_state.json"
_DEFAULT_PROJECT = "Default"


class _PersistentStore:
    """JSON-backed store of per-project field values.

    On-disk shape:
        {
          "current_project": "<name>",
          "projects": { "<name>": {"<key>": <value>, ...}, ... }
        }

    A legacy flat `{"<key>": <value>}` file is migrated on load into a
    single "Default" project.
    """

    def __init__(self, path):
        self.path = path
        self.projects = {}
        self.current_project = _DEFAULT_PROJECT
        self._listeners = []  # called on switch/clone/delete after data change
        self._load()

    def _load(self):
        loaded = None
        try:
            with open(self.path) as f:
                loaded = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            loaded = None
        if isinstance(loaded, dict) and isinstance(loaded.get("projects"), dict):
            self.projects = {
                str(k): dict(v) for k, v in loaded["projects"].items()
                if isinstance(v, dict)
            }
            cur = loaded.get("current_project")
            if isinstance(cur, str) and cur in self.projects:
                self.current_project = cur
            elif self.projects:
                self.current_project = sorted(self.projects)[0]
        elif isinstance(loaded, dict) and loaded:
            # Legacy flat schema — migrate into a single Default project.
            self.projects = {_DEFAULT_PROJECT: dict(loaded)}
            self.current_project = _DEFAULT_PROJECT
        if not self.projects:
            self.projects = {_DEFAULT_PROJECT: {}}
            self.current_project = _DEFAULT_PROJECT

    def _persist(self):
        try:
            with open(self.path, "w") as f:
                json.dump(
                    {"current_project": self.current_project,
                     "projects": self.projects},
                    f, indent=2, sort_keys=True,
                )
        except OSError:
            pass

    def _current(self):
        return self.projects.setdefault(self.current_project, {})

    # -- key/value scoped to current project --

    def get(self, key, default):
        return self._current().get(key, default)

    def set(self, key, value):
        bucket = self._current()
        if bucket.get(key) == value:
            return
        bucket[key] = value
        self._persist()

    # -- project management --

    def list_projects(self):
        return sorted(self.projects.keys(), key=str.lower)

    def add_listener(self, fn):
        """`fn()` is called whenever the current project changes (switch/clone/
        delete) and tabs should reload their variables from the new project."""
        self._listeners.append(fn)

    def _notify(self):
        for fn in self._listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001 - listener isolation
                traceback.print_exc()

    def switch_project(self, name):
        if name not in self.projects or name == self.current_project:
            return
        self.current_project = name
        self._persist()
        self._notify()

    def clone_project(self, new_name):
        """Duplicate the current project's data under `new_name` and switch."""
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("Project name cannot be empty.")
        if new_name in self.projects:
            raise ValueError(f"Project '{new_name}' already exists.")
        self.projects[new_name] = dict(self._current())
        self.current_project = new_name
        self._persist()
        self._notify()

    def delete_project(self, name):
        """Remove `name`. If it was current, switch to another (creating the
        Default project again if it's the last one)."""
        if name not in self.projects:
            return
        del self.projects[name]
        if not self.projects:
            self.projects[_DEFAULT_PROJECT] = {}
            self.current_project = _DEFAULT_PROJECT
        elif name == self.current_project:
            self.current_project = self.list_projects()[0]
        self._persist()
        self._notify()


_store = _PersistentStore(_STATE_PATH)


# ---------------------------------------------------------------------------
# Global (per-project) fields shared across all tabs
# ---------------------------------------------------------------------------

_GLOBAL_FOLDER_KEY = "_global.image_folder"
_global_folder_var = None  # tk.StringVar, created by _build_project_bar


def _coerce_stored(var_class, value, default):
    if var_class is tk.StringVar:
        if isinstance(value, str):
            return value
        return "" if value is None else str(value)
    if var_class is tk.BooleanVar:
        return bool(value) if not isinstance(value, bool) else value
    return value if value is not None else default


def _make_global_folder_var():
    """Create the module-level global folder tk.StringVar (idempotent).

    The variable is persisted under `_GLOBAL_FOLDER_KEY` in the current
    project's bucket, and refreshed automatically when the current project
    changes.
    """
    global _global_folder_var
    if _global_folder_var is not None:
        return _global_folder_var
    stored = _coerce_stored(tk.StringVar, _store.get(_GLOBAL_FOLDER_KEY, ""), "")
    var = tk.StringVar(value=stored)
    var.trace_add(
        "write", lambda *a: _store.set(_GLOBAL_FOLDER_KEY, var.get()))

    def _reload():
        val = _coerce_stored(tk.StringVar, _store.get(_GLOBAL_FOLDER_KEY, ""), "")
        var.set(val)

    _store.add_listener(_reload)
    _global_folder_var = var
    return var


def _current_image_folder():
    """Return the currently-selected image folder (stripped) or ''."""
    if _global_folder_var is None:
        return ""
    return _global_folder_var.get().strip()


# ---------------------------------------------------------------------------
# Other shared, per-project fields shown in the project bar (music, title,
# subtitle and their fonts). Like the image folder, each is persisted per
# project and reloaded when the current project changes.
# ---------------------------------------------------------------------------

_DEFAULT_MUSIC = (
    "/Users/markusvoelter/Documents/projects/photo.voelter.de/media/ai-music")

_GLOBAL_MUSIC_KEY = "_global.music"
_GLOBAL_TITLE_KEY = "_global.title"
_GLOBAL_TITLE_FONT_KEY = "_global.title_font"
_GLOBAL_SUBTITLE_KEY = "_global.subtitle"
_GLOBAL_SUBTITLE_FONT_KEY = "_global.subtitle_font"
_GLOBAL_TEMPLATE_SET_KEY = "_global.template_set"
_GLOBAL_SELECTED_TAB_KEY = "_global.selected_tab"

_global_vars = {}  # key -> tk.StringVar, created lazily by _make_global_var


def _make_global_var(key, default=""):
    """Return a cached, per-project-persisted tk.StringVar for `key`."""
    if key in _global_vars:
        return _global_vars[key]
    stored = _coerce_stored(tk.StringVar, _store.get(key, default), default)
    var = tk.StringVar(value=stored)
    var.trace_add("write", lambda *a: _store.set(key, var.get()))

    def _reload():
        var.set(_coerce_stored(tk.StringVar, _store.get(key, default), default))

    _store.add_listener(_reload)
    _global_vars[key] = var
    return var


def _current_music():
    return _make_global_var(_GLOBAL_MUSIC_KEY, _DEFAULT_MUSIC).get().strip()


def _current_title():
    return _make_global_var(_GLOBAL_TITLE_KEY).get().strip()


def _current_title_font():
    return _make_global_var(_GLOBAL_TITLE_FONT_KEY).get().strip()


def _current_subtitle():
    return _make_global_var(_GLOBAL_SUBTITLE_KEY).get().strip()


def _current_subtitle_font():
    return _make_global_var(_GLOBAL_SUBTITLE_FONT_KEY).get().strip()


def _default_template_set():
    """Preferred default template set: "mvp" if present, else the first one."""
    sets = list_template_sets()
    if "mvp" in sets:
        return "mvp"
    return sets[0] if sets else ""


def _current_template_set():
    return _make_global_var(
        _GLOBAL_TEMPLATE_SET_KEY, _default_template_set()).get().strip() or None


def _selected_tab_label(nb):
    """The text label of the notebook's current tab, or '' if none."""
    try:
        return nb.tab(nb.select(), "text")
    except tk.TclError:
        return ""


def _save_selected_tab(nb):
    """Persist the current tab label in the current project's store."""
    label = _selected_tab_label(nb)
    if label:
        _store.set(_GLOBAL_SELECTED_TAB_KEY, label)


def _restore_selected_tab(nb):
    """Select the tab stored for the current project, if it still exists."""
    label = _store.get(_GLOBAL_SELECTED_TAB_KEY, "")
    if not label:
        return
    for tab_id in nb.tabs():
        if nb.tab(tab_id, "text") == label:
            nb.select(tab_id)
            return


def _pick_music_into(var, parent=None):
    """Open an audio-file picker, storing the choice in `var`."""
    current = var.get().strip()
    if current and os.path.isfile(current):
        initial_dir = os.path.dirname(current)
    elif current and os.path.isdir(current):
        initial_dir = current
    else:
        initial_dir = os.path.expanduser("~")
    path = filedialog.askopenfilename(
        initialdir=initial_dir,
        filetypes=[("Audio", "*.mp3 *.m4a *.wav *.flac *.aac *.ogg *.opus"),
                   ("All files", "*.*")],
        title="Pick an audio file", parent=parent)
    if path:
        var.set(path)


def _pick_font_into(var, parent=None):
    """Open a font-file picker, storing the choice in `var`."""
    current = var.get().strip()
    if current and os.path.isfile(current):
        initial_dir = os.path.dirname(current)
    elif sys.platform == "darwin":
        initial_dir = "/System/Library/Fonts"
    else:
        initial_dir = os.path.expanduser("~")
    path = filedialog.askopenfilename(
        initialdir=initial_dir,
        filetypes=[("Font files", "*.ttf *.ttc *.otf"), ("All files", "*.*")],
        title="Pick a font file", parent=parent)
    if path:
        var.set(path)


# ---------------------------------------------------------------------------
# Background image-folder watcher
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
_FOLDER_CHECK_INTERVAL_MS = 2000


def _folder_status(path):
    """Classify the selected image folder without raising.

    Returns a `(state, detail)` tuple where state is one of:
      "empty"      nothing selected yet          (detail "")
      "ok"         folder exists and has images  (detail = image count, int)
      "missing"    path is gone or unreadable    (detail = message)
      "no_images"  folder exists but is empty    (detail = message)
    """
    if not path:
        return ("empty", "")
    if not os.path.isdir(path):
        return ("missing", "folder not found")
    count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_file = entry.is_file()
                except OSError:
                    is_file = False
                if (is_file and os.path.splitext(entry.name)[1].lower()
                        in _IMAGE_EXTENSIONS):
                    count += 1
    except OSError:
        return ("missing", "folder not readable")
    if count == 0:
        return ("no_images", "no images in folder")
    return ("ok", count)


class _FolderWatcher:
    """Periodically check the selected image folder and flag problems.

    The filesystem scan runs on a daemon thread so a slow or disconnected
    network folder never freezes the UI; results are marshalled back to the Tk
    thread through a queue and rendered into `label` (green tick when images
    are present, red warning when the folder has vanished or is empty). A
    change to `folder_var` wakes the worker for a prompt re-check.
    """

    def __init__(self, root, folder_var, label,
                 interval_ms=_FOLDER_CHECK_INTERVAL_MS):
        self.root = root
        self.folder_var = folder_var
        self.label = label
        self.interval_ms = interval_ms
        self._path = folder_var.get().strip()
        self._queue = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        folder_var.trace_add("write", self._on_var_change)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self.check_now()  # render an initial status synchronously
        self.root.after(self.interval_ms, self._poll)

    def _on_var_change(self, *_a):
        # Runs on the Tk thread; hand the new path to the worker and nudge it.
        self._path = self.folder_var.get().strip()
        self._wake.set()
        if not self._stop.is_set():
            self.root.after(150, self._drain)

    def _worker(self):
        while not self._stop.is_set():
            self._queue.put(_folder_status(self._path))
            # Wake early when the selection changed; else poll on the interval.
            self._wake.wait(self.interval_ms / 1000.0)
            self._wake.clear()

    def _poll(self):
        self._drain()
        if not self._stop.is_set():
            self.root.after(self.interval_ms, self._poll)

    def _drain(self):
        """Apply the most recent status posted by the worker (Tk thread)."""
        latest = None
        try:
            while True:
                latest = self._queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._apply(*latest)

    def check_now(self):
        """Scan and render synchronously (initial state; also used in tests)."""
        self._apply(*_folder_status(self.folder_var.get().strip()))

    def _apply(self, state, detail):
        if state == "ok":
            self.label.config(text=f"✓ {detail} image(s)",
                              foreground="#4caf50")
        elif state == "empty":
            self.label.config(text="", foreground="")
        else:  # "missing" or "no_images"
            self.label.config(text=f"⚠ {detail}", foreground="#e05252")

    def stop(self):
        self._stop.set()
        self._wake.set()


# ---------------------------------------------------------------------------
# Base tab
# ---------------------------------------------------------------------------

class RunnerTab(ttk.Frame):
    """Base tab that runs a tool function in a background thread.

    Subclasses set `persist_prefix` to a unique string; `self.pvar(name, ...)`
    then creates a `tk.Variable` whose value is loaded from the JSON store on
    creation and saved back on every change.
    """

    persist_prefix = None  # subclasses override

    # Every tab stores its result in the selected images folder, named
    # "<project> <output_label> <timestamp>". `output_ext` is the file
    # extension for single-file outputs, or None for tools that write a
    # directory of files.
    output_label = None
    output_ext = None

    def _default_output(self, folder):
        """Default output path inside the input `folder`, named after the
        current project, this tool, and a timestamp."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{_store.current_project} {self.output_label} {ts}"
        if self.output_ext:
            name += self.output_ext
        return os.path.join(folder, name)

    def __init__(self, master, run_fn, default_input_dir=None):
        super().__init__(master, padding=10)
        self.run_fn = run_fn
        self.default_input_dir = default_input_dir
        self.ctx = None
        self.worker = None
        self.log_queue = queue.Queue()
        self.last_output = None
        # (store_key, tk_var, default_value) entries used to reload the tab's
        # inputs when the globally selected project changes.
        self._pvars = []
        self._build_form()
        self._build_log()
        self.after(80, self._drain_log)
        _store.add_listener(self._reload_from_store)

    def pvar(self, name, default, var_class=tk.StringVar):
        """Return a tk.Variable bound to the current project's JSON store.

        The store key is f"{self.persist_prefix}.{name}". If `persist_prefix`
        is None (e.g. for a tab that opts out), the variable is created with
        the supplied default and no persistence wiring.
        """
        if self.persist_prefix is None:
            return var_class(value=default)
        key = f"{self.persist_prefix}.{name}"
        stored = _coerce_stored(var_class, _store.get(key, default), default)
        var = var_class(value=stored)
        var.trace_add("write", lambda *a: _store.set(key, var.get()))
        self._pvars.append((key, var, default))
        return var

    def _reload_from_store(self):
        """Called when the current project changes; refresh every pvar from
        the newly-selected project (falling back to its original default)."""
        for key, var, default in self._pvars:
            stored = _coerce_stored(
                type(var), _store.get(key, default), default)
            var.set(stored)
        self._reload_extras()

    def _reload_extras(self):
        """Override to reload non-pvar persistent widgets (e.g. multiline
        Text boxes) when the current project changes."""
        pass

    # to be overridden
    def _build_form(self):
        raise NotImplementedError

    def _gather_kwargs(self):
        """Return kwargs dict for run_fn, or raise ValueError with a user message."""
        raise NotImplementedError

    # shared chrome
    def _build_log(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(10, 4))
        self.run_btn = ttk.Button(bar, text="Run", command=self._on_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop",
                                   command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.clear_btn = ttk.Button(bar, text="Clear log",
                                    command=self._clear_log)
        self.clear_btn.pack(side="left", padx=(6, 0))
        self.open_btn = ttk.Button(bar, text="Reveal output",
                                   command=self._open_output, state="disabled")
        self.open_btn.pack(side="left", padx=(6, 0))
        self.status = ttk.Label(bar, text="Idle")
        self.status.pack(side="left", padx=(12, 0))

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=14, wrap="word",
                           bg="#1e1e1e", fg="#e0e0e0",
                           insertbackground="#e0e0e0",
                           font=("Menlo", 11))
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

    def _clear_log(self):
        self.log.delete("1.0", "end")

    def _append_log(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def _push_log(self, msg):
        self.log_queue.put(msg + "\n")

    def _drain_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if item is None:
                    self._on_done()
                else:
                    self._append_log(item)
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    def _on_run(self):
        if self.worker is not None:
            return
        try:
            kwargs = self._gather_kwargs()
        except ValueError as e:
            self._append_log(f"[error] {e}\n")
            return

        self._append_log(f"\n--- run {self.run_fn.__module__}.{self.run_fn.__name__}("
                         + ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
                         + ") ---\n")
        self.ctx = RunContext(log=self._push_log)
        kwargs["ctx"] = self.ctx
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self.status.config(text="Running...")
        self.last_output = None

        self.worker = threading.Thread(target=self._make_target(kwargs), daemon=True)
        self.worker.start()

    def _make_target(self, kwargs):
        """Build the worker thread target. Default: call run_fn once."""
        def target():
            try:
                result = self.run_fn(**kwargs)
                self.last_output = result
            except RunCancelled:
                self._push_log("[cancelled]")
            except Exception as e:
                self._push_log(f"[error] {e}")
                self._push_log(traceback.format_exc())
            finally:
                self.log_queue.put(None)
        return target

    def _on_done(self):
        self.worker = None
        self.run_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if self.last_output:
            self.open_btn.config(state="normal")
        if self.ctx and self.ctx.cancelled():
            self.status.config(text="Cancelled")
        else:
            self.status.config(text="Done" if self.last_output else "Error")
        self.ctx = None

    def _on_stop(self):
        if self.ctx is None:
            return
        self.ctx.cancel()
        self.status.config(text="Stopping...")

    def _open_output(self):
        reveal_output(self.last_output)


# ---------------------------------------------------------------------------
# Collage
# ---------------------------------------------------------------------------

class CollageTab(RunnerTab):
    persist_prefix = "collage"
    output_label = "collage"
    output_ext = ".jpg"

    def __init__(self, master):
        super().__init__(master, collage_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imageCollage"))

    def _build_form(self):
        self.style = self.pvar("style", "mosaic")
        self.repetitions = self.pvar("repetitions", "5")
        self.output = self.pvar("output", "")
        self.num_cols = self.pvar("num_cols", "")
        self.aspect = self.pvar("aspect", "16:9")
        self.count = self.pvar("count", "")
        self.bg = self.pvar("bg", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Style").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.style,
                     values=list(collage_mod.STYLES),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Repetitions").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.repetitions, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Num columns (columns style)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_cols, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Aspect ratio (mosaic)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.aspect,
                     values=["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"]
                     ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Image count (mosaic, optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.count, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Background color hex (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.bg, width=12).grid(row=r, column=1, sticky="w", padx=4)
        ttk.Button(grid, text="Pick color...",
                   command=lambda: pick_color(self.bg)).grid(row=r, column=2)
        r += 1

    def _pick_output(self):
        ensure_output_dir()
        path = filedialog.asksaveasfilename(
            defaultextension=".jpg",
            filetypes=[("JPEG", "*.jpg"), ("All files", "*.*")],
            initialdir=OUTPUT_DIR,
        )
        if path:
            self.output.set(path)

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        if not self.repetitions.get().strip():
            raise ValueError("Enter repetitions.")
        try:
            reps = int(self.repetitions.get())
        except ValueError:
            raise ValueError("Repetitions must be a positive integer.")
        if reps < 1:
            raise ValueError("Repetitions must be at least 1.")
        self._reps = reps
        kw = {
            "folder": folder,
            "style": self.style.get(),
        }
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        else:
            kw["output"] = self._default_output(folder)
        if self.num_cols.get().strip():
            try:
                kw["num_cols"] = int(self.num_cols.get())
            except ValueError:
                raise ValueError("Num columns must be an integer.")
        if self.style.get() == "mosaic":
            if self.aspect.get().strip():
                kw["aspect"] = self.aspect.get().strip()
            if self.count.get().strip():
                try:
                    kw["count"] = int(self.count.get())
                except ValueError:
                    raise ValueError("Image count must be an integer.")
        if self.bg.get().strip():
            kw["bg"] = self.bg.get().strip()
        return kw

    def _make_target(self, kwargs):
        reps = self._reps
        user_output = kwargs.pop("output", None)

        def target():
            try:
                last = None
                for i in range(reps):
                    if self.ctx.cancelled():
                        break
                    if reps > 1:
                        self._push_log(f"\n=== Run {i + 1}/{reps} ===")
                    run_kwargs = dict(kwargs)
                    if user_output:
                        if reps > 1:
                            base, ext = os.path.splitext(user_output)
                            run_kwargs["output"] = f"{base}_run{i + 1}{ext}"
                        else:
                            run_kwargs["output"] = user_output
                    elif reps > 1:
                        run_kwargs["seq"] = i + 1
                    last = self.run_fn(**run_kwargs)
                self.last_output = last
            except RunCancelled:
                self._push_log("[cancelled]")
            except Exception as e:
                self._push_log(f"[error] {e}")
                self._push_log(traceback.format_exc())
            finally:
                self.log_queue.put(None)
        return target


# ---------------------------------------------------------------------------
# Film Strip
# ---------------------------------------------------------------------------

class FilmstripTab(RunnerTab):
    persist_prefix = "filmstrip"
    output_label = "film strip"
    output_ext = ".jpg"

    def __init__(self, master):
        super().__init__(master, filmstrip_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imageCollage"))

    def _build_form(self):
        self.num_columns = self.pvar("num_columns", "2")
        self.repetitions = self.pvar("repetitions", "1")
        self.allow_repeat = self.pvar("allow_repeat", False, tk.BooleanVar)
        self.crop = self.pvar("crop", True, tk.BooleanVar)
        self.bg = self.pvar("bg", "")
        self.output = self.pvar("output", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Number of columns").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_columns, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Repetitions").grid(row=r, column=0, sticky="w", pady=2)
        rep_row = ttk.Frame(grid)
        rep_row.grid(row=r, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Entry(rep_row, textvariable=self.repetitions, width=10).pack(side="left")
        ttk.Checkbutton(rep_row, text="allow photos to repeat across outputs",
                        variable=self.allow_repeat).pack(side="left", padx=(10, 0))
        r += 1

        ttk.Checkbutton(grid,
                        text="Crop to 9:16 (off = keep width, height follows "
                             "each image's aspect)",
                        variable=self.crop
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Label(grid, text="Background color hex (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.bg, width=12).grid(row=r, column=1, sticky="w", padx=4)
        ttk.Button(grid, text="Pick color...",
                   command=lambda: pick_color(self.bg)).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid,
                  text="(output is 9:16, auto-filled with as many random photos "
                       "as fit; background defaults to dark grey; repetitions "
                       "make several images without reusing the same photo)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _pick_output(self):
        ensure_output_dir()
        path = filedialog.asksaveasfilename(
            defaultextension=".jpg",
            filetypes=[("JPEG", "*.jpg"), ("All files", "*.*")],
            initialdir=OUTPUT_DIR,
        )
        if path:
            self.output.set(path)

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        try:
            num_columns = int(self.num_columns.get())
        except ValueError:
            raise ValueError("Number of columns must be an integer.")
        if num_columns < 1:
            raise ValueError("Number of columns must be at least 1.")
        try:
            repetitions = int(self.repetitions.get())
        except ValueError:
            raise ValueError("Repetitions must be an integer.")
        if repetitions < 1:
            raise ValueError("Repetitions must be at least 1.")
        kw = {
            "folder": folder,
            "num_columns": num_columns,
            "repetitions": repetitions,
            "allow_repeat": self.allow_repeat.get(),
            "crop": self.crop.get(),
        }
        if self.bg.get().strip():
            kw["bg"] = self.bg.get().strip()
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        else:
            kw["output"] = self._default_output(folder)
        return kw


# ---------------------------------------------------------------------------
# Rotate Video
# ---------------------------------------------------------------------------

class RotateVideoTab(RunnerTab):
    persist_prefix = "rotate_video"
    output_label = "rotate video"
    output_ext = ".mp4"

    def __init__(self, master):
        super().__init__(master, rotate_video_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imageRotateVideo"))

    def _build_form(self):
        self.duration = self.pvar("duration", "15")
        self.cover = self.pvar("cover", "")
        self.output = self.pvar("output", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Total duration (seconds)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.duration, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Cover image").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.cover).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...", command=self._pick_cover
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="(cover must be inside the chosen image folder)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _pick_cover(self):
        folder = _current_image_folder()
        initial = folder or self.default_input_dir
        path = filedialog.askopenfilename(
            initialdir=initial,
            filetypes=[("Image", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
        )
        if not path:
            return
        if folder and os.path.dirname(os.path.abspath(path)) == os.path.abspath(folder):
            self.cover.set(os.path.basename(path))
        elif not folder:
            # First-time: promote the picked file's directory into the
            # project's global images folder.
            if _global_folder_var is not None:
                _global_folder_var.set(os.path.dirname(path))
            self.cover.set(os.path.basename(path))
        else:
            self.cover.set(path)

    def _pick_output(self):
        ensure_output_dir()
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("All files", "*.*")],
            initialdir=OUTPUT_DIR,
        )
        if path:
            self.output.set(path)

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        if not self.duration.get().strip():
            raise ValueError("Enter a duration.")
        try:
            duration = float(self.duration.get())
        except ValueError:
            raise ValueError(f"Duration must be a number, got: {self.duration.get()}")
        if not self.cover.get().strip():
            raise ValueError("Pick the cover image.")

        kw = {
            "folder": folder,
            "total_duration_seconds": duration,
            "cover_image": self.cover.get().strip(),
        }
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        else:
            kw["output"] = self._default_output(folder)
        music = _current_music()
        if music:
            kw["music"] = music
        return kw


# ---------------------------------------------------------------------------
# Carousel
# ---------------------------------------------------------------------------

class CarouselTab(RunnerTab):
    persist_prefix = "carousel"
    output_label = "carousel"  # directory of slides (no extension)

    def __init__(self, master):
        super().__init__(master, carousel_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imagesSwipeys2"))

    def _build_form(self):
        self.num_slides = self.pvar("num_slides", "20")
        self.aspect = self.pvar("aspect", "9:16")
        self.output_dir = self.pvar("output_dir", "")
        self.random_order = self.pvar("random_order", False, tk.BooleanVar)
        self.num_sets = self.pvar("num_sets", "1")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Number of slides").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_slides, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Checkbutton(grid,
                        text="Random order (off = alphabetical by filename)",
                        variable=self.random_order
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Label(grid, text="Number of sets").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_sets, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="(>1 writes each set to set_NN/ under the image folder; "
                             "combine with random order for different results)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        ttk.Label(grid, text="Aspect ratio").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.aspect,
                     values=list(carousel_mod.ASPECT_RATIOS.keys()),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Output folder (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output_dir).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.output_dir, OUTPUT_DIR)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="(default output is a sibling folder named <input>-swipey)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        if not self.num_slides.get().strip():
            raise ValueError("Enter the number of slides.")
        try:
            n = int(self.num_slides.get())
        except ValueError:
            raise ValueError("Number of slides must be an integer.")
        try:
            num_sets = int(self.num_sets.get() or "1")
        except ValueError:
            raise ValueError("Number of sets must be an integer.")
        if num_sets < 1:
            raise ValueError("Number of sets must be at least 1.")
        kw = {
            "folder": folder,
            "num_slides": n,
            "aspect_ratio": self.aspect.get(),
            "random_order": self.random_order.get(),
            "num_sets": num_sets,
        }
        if self.output_dir.get().strip():
            kw["output_dir"] = self.output_dir.get().strip()
        else:
            kw["output_dir"] = self._default_output(folder)
        return kw


# ---------------------------------------------------------------------------
# Scroll Video
# ---------------------------------------------------------------------------

class ScrollVideoTab(RunnerTab):
    persist_prefix = "scroll_video"
    output_label = "scroll video"
    output_ext = ".mp4"

    def __init__(self, master):
        super().__init__(master, scroll_video_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imagesSwipeys2"))

    def _build_form(self):
        self.aspect = self.pvar("aspect", "9:16")
        self.output = self.pvar("output", "")
        self.scroll_mode = self.pvar("scroll_mode", "Continuous pan")
        self.stepped_hold_s = self.pvar("stepped_hold_s", "2.0")
        self.scroll_speed_pct = self.pvar("scroll_speed_pct", "200")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Aspect ratio").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.aspect,
                     values=list(scroll_video_mod.ASPECT_RATIOS.keys()),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Scroll mode").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.scroll_mode,
                     values=["Continuous pan", "Hold each slide"],
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Hold per slide (s, stepped only)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.stepped_hold_s, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Scroll speed %").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.scroll_speed_pct, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

    def _pick_output(self):
        ensure_output_dir()
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("All files", "*.*")],
            initialdir=OUTPUT_DIR,
        )
        if path:
            self.output.set(path)

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        mode_map = {
            "Continuous pan": scroll_video_mod.MODE_CONTINUOUS,
            "Hold each slide": scroll_video_mod.MODE_STEPPED,
        }
        kw = {
            "folder": folder,
            "aspect_ratio": self.aspect.get(),
            "scroll_mode": mode_map.get(self.scroll_mode.get(),
                                        scroll_video_mod.MODE_CONTINUOUS),
        }
        if self.stepped_hold_s.get().strip():
            try:
                hold = float(self.stepped_hold_s.get())
            except ValueError:
                raise ValueError("Hold per slide must be a number.")
            if hold <= 0:
                raise ValueError("Hold per slide must be > 0.")
            kw["stepped_hold_s"] = hold
        if self.scroll_speed_pct.get().strip():
            try:
                pct = float(self.scroll_speed_pct.get())
            except ValueError:
                raise ValueError("Scroll speed % must be a number.")
            if pct <= 0:
                raise ValueError("Scroll speed % must be > 0.")
            kw["scroll_speed_pct"] = pct
        music = _current_music()
        if music:
            kw["music"] = music
        end_screen = template_for(self.aspect.get(), "end-screen",
                                  template_set=_current_template_set())
        if end_screen:
            kw["end_screen"] = end_screen
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        else:
            kw["output"] = self._default_output(folder)
        return kw


# ---------------------------------------------------------------------------
# Fast Scroll
# ---------------------------------------------------------------------------

class ShuffleRevealTab(RunnerTab):
    persist_prefix = "shuffle_reveal"
    output_label = "fast scroll"
    output_ext = ".mp4"

    def __init__(self, master):
        super().__init__(master, shuffle_reveal_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imagesSwipeys2"))

    def _build_form(self):
        self.aspect = self.pvar("aspect", "9:16")
        self.direction = self.pvar("direction", shuffle_reveal_mod.DIRECTION_HORIZONTAL)
        self.hold_s = self.pvar("hold_s", "1.5")
        self.num_images = self.pvar("num_images", "0")
        self.min_intermediate = self.pvar(
            "min_intermediate", str(shuffle_reveal_mod.DEFAULT_MIN_INTERMEDIATE))
        self.max_intermediate = self.pvar(
            "max_intermediate", str(shuffle_reveal_mod.DEFAULT_MAX_INTERMEDIATE))
        self.random_order = self.pvar("random_order", False, tk.BooleanVar)
        self.reverse = self.pvar("reverse", False, tk.BooleanVar)
        self.output = self.pvar("output", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Aspect ratio").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.aspect,
                     values=list(shuffle_reveal_mod.ASPECT_RATIOS.keys()),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Direction").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.direction,
                     values=list(shuffle_reveal_mod.DIRECTIONS),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Hold per image (s)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.hold_s, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Number of images (0 = all)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_images, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Intermediate images (min)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.min_intermediate, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Intermediate images (max)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.max_intermediate, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Checkbutton(grid,
                        text="Randomize order (off = sorted by filename)",
                        variable=self.random_order
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Checkbutton(grid,
                        text="Reverse (randomly scroll some steps the opposite way)",
                        variable=self.reverse
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid,
                  text="(between each image, a random number of images in the "
                       "min-max range whip past very fast)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _pick_output(self):
        ensure_output_dir()
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("All files", "*.*")],
            initialdir=OUTPUT_DIR,
        )
        if path:
            self.output.set(path)

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        kw = {
            "folder": folder,
            "aspect_ratio": self.aspect.get(),
            "direction": self.direction.get(),
        }
        if self.hold_s.get().strip():
            try:
                hold = float(self.hold_s.get())
            except ValueError:
                raise ValueError("Hold per image must be a number.")
            if hold <= 0:
                raise ValueError("Hold per image must be > 0.")
            kw["hold_s"] = hold
        if self.num_images.get().strip():
            try:
                num_images = int(self.num_images.get())
            except ValueError:
                raise ValueError("Number of images must be an integer.")
            if num_images < 0:
                raise ValueError("Number of images must be >= 0.")
            kw["num_images"] = num_images
        try:
            min_int = int(self.min_intermediate.get())
            max_int = int(self.max_intermediate.get())
        except ValueError:
            raise ValueError("Intermediate image counts must be integers.")
        if min_int < 0 or max_int < 0:
            raise ValueError("Intermediate image counts must be >= 0.")
        if max_int < min_int:
            raise ValueError("Max intermediate images must be >= min.")
        kw["min_intermediate"] = min_int
        kw["max_intermediate"] = max_int
        kw["random_order"] = self.random_order.get()
        kw["reverse"] = self.reverse.get()
        music = _current_music()
        if music:
            kw["music"] = music
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        else:
            kw["output"] = self._default_output(folder)
        return kw


# ---------------------------------------------------------------------------
# Reel
# ---------------------------------------------------------------------------

class ReelTab(RunnerTab):
    persist_prefix = "reel"
    output_label = "reel"
    output_ext = ".mp4"

    def __init__(self, master):
        super().__init__(master, reel_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "vertHorizVideo"))

    def _build_form(self):
        self.interval = self.pvar("interval", "2.0")
        self.beats_per_transition = self.pvar("beats_per_transition", "4")
        self.output = self.pvar("output", "")
        self.bg = self.pvar("bg", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Interval per image (seconds)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.interval, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Beats per transition (0 = off)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.beats_per_transition, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Background color hex (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.bg, width=12).grid(row=r, column=1, sticky="w", padx=4)
        ttk.Button(grid, text="Pick color...",
                   command=lambda: pick_color(self.bg)).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

    def _pick_output(self):
        ensure_output_dir()
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("All files", "*.*")],
            initialdir=OUTPUT_DIR,
        )
        if path:
            self.output.set(path)

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        kw = {"folder": folder}
        if self.interval.get().strip():
            try:
                kw["interval"] = float(self.interval.get())
            except ValueError:
                raise ValueError(f"Interval must be a number.")
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        else:
            kw["output"] = self._default_output(folder)
        if self.bg.get().strip():
            kw["bg"] = self.bg.get().strip()
        music = _current_music()
        if music:
            kw["music"] = music
        # Reels are vertical — resolve the 9:16 end-screen template.
        end_screen = template_for("9:16", "end-screen",
                                  template_set=_current_template_set())
        if end_screen:
            kw["end_screen"] = end_screen
        if self.beats_per_transition.get().strip():
            try:
                bpt = int(self.beats_per_transition.get())
            except ValueError:
                raise ValueError("Beats per transition must be an integer.")
            if bpt < 0:
                raise ValueError("Beats per transition must be >= 0.")
            if bpt > 0:
                kw["beats_per_transition"] = bpt
        return kw


# ---------------------------------------------------------------------------
# Walls
# ---------------------------------------------------------------------------

class WallsTab(RunnerTab):
    persist_prefix = "walls"
    output_label = "walls"  # directory of composites (no extension)

    def __init__(self, master):
        super().__init__(master, walls_mod.run,
                         default_input_dir=PROJECT_ROOT)

    def _build_form(self):
        self.wall_folder = self.pvar("wall_folder", WALLS_DIR)
        self.num_outputs = self.pvar("num_outputs", "10")
        self.output_dir = self.pvar("output_dir", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Wall folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.wall_folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.wall_folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Number of outputs").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_outputs, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Output folder (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output_dir).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.output_dir, OUTPUT_DIR)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid,
                  text="(default output is a timestamped subfolder of output/)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _gather_kwargs(self):
        image_folder = _current_image_folder()
        if not self.wall_folder.get().strip():
            raise ValueError("Pick a wall folder.")
        if not image_folder:
            raise ValueError("Pick an images folder in the project bar.")
        if not self.num_outputs.get().strip():
            raise ValueError("Enter the number of outputs.")
        try:
            n = int(self.num_outputs.get())
        except ValueError:
            raise ValueError("Number of outputs must be an integer.")
        if n < 1:
            raise ValueError("Number of outputs must be at least 1.")
        kw = {
            "wall_folder": self.wall_folder.get().strip(),
            "image_folder": image_folder,
            "num_outputs": n,
        }
        if self.output_dir.get().strip():
            kw["output_dir"] = self.output_dir.get().strip()
        else:
            kw["output_dir"] = self._default_output(image_folder)
        return kw


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

class SplitTab(RunnerTab):
    persist_prefix = "split"
    output_label = "split"  # parent directory of per-image slide folders

    def __init__(self, master):
        super().__init__(master, split_mod.run,
                         default_input_dir=PROJECT_ROOT)

    def _build_form(self):
        self.aspect_ratio = self.pvar("aspect_ratio", "9:16")
        self.output_dir = self.pvar("output_dir", "")
        self.bg = self.pvar("bg", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Aspect ratio").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.aspect_ratio,
                     values=list(split_mod.ASPECT_RATIOS.keys()),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Output folder (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output_dir).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.output_dir, OUTPUT_DIR)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Padding color hex (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.bg, width=12).grid(row=r, column=1, sticky="w", padx=4)
        ttk.Button(grid, text="Pick color...",
                   command=lambda: pick_color(self.bg)).grid(row=r, column=2)
        r += 1

        ttk.Label(grid,
                  text="(every image in the folder is split; slides keep original height, "
                       "width = height × aspect ratio. Output subfolders sit next to each "
                       "source image unless an output folder is set. Padding color is only "
                       "used if an input is narrower than one slide.)",
                  foreground="gray", wraplength=560, justify="left"
                  ).grid(row=r, column=0, columnspan=3, sticky="w")

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        kw = {
            "folder": folder,
            "aspect_ratio": self.aspect_ratio.get(),
        }
        if self.output_dir.get().strip():
            kw["output_dir"] = self.output_dir.get().strip()
        else:
            kw["output_dir"] = self._default_output(folder)
        if self.bg.get().strip():
            kw["bg"] = self.bg.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Ken Burns
# ---------------------------------------------------------------------------

class KenBurnsTab(RunnerTab):
    persist_prefix = "ken_burns"
    output_label = "ken burns"
    output_ext = ".mp4"

    def __init__(self, master):
        super().__init__(master, ken_burns_mod.run,
                         default_input_dir=PROJECT_ROOT)

    def _build_form(self):
        self.num_images = self.pvar("num_images", "20")
        self.aspect = self.pvar("aspect", "16:9")
        self.duration = self.pvar("duration", "4.0")
        self.kb_strength_pct = self.pvar("kb_strength_pct", "50")
        self.gimmick = self.pvar("gimmick", False, tk.BooleanVar)
        self.random_order = self.pvar("random_order", True, tk.BooleanVar)
        self.start_at_crop = self.pvar("start_at_crop", False, tk.BooleanVar)
        self.debug = self.pvar("debug", False, tk.BooleanVar)
        self.text_slide_font = self.pvar("text_slide_font", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Number of images").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_images, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Aspect ratio").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.aspect,
                     values=list(ken_burns_mod.ASPECTS.keys()),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        self.duration_label = ttk.Label(grid, text="Duration per image (s)")
        self.duration_label.grid(row=r, column=0, sticky="w", pady=2)
        self.duration_entry = ttk.Entry(grid, textvariable=self.duration, width=10)
        self.duration_entry.grid(row=r, column=1, sticky="w", padx=4)
        self.duration_hint = ttk.Label(grid, text="", foreground="gray")
        self.duration_hint.grid(row=r, column=2, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Ken Burns strength %").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.kb_strength_pct, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Checkbutton(grid,
                        text="Gimmick intro: flip-through of all selected images "
                             "(0.05s each, music fades in)",
                        variable=self.gimmick
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Checkbutton(grid,
                        text="Random order (off = alphabetical by filename)",
                        variable=self.random_order
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Checkbutton(grid,
                        text="Start at crop (use the 'crop' event in the "
                             "sidecar JSON as t=0)",
                        variable=self.start_at_crop
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Checkbutton(grid,
                        text="Debug (overlay live music/JSON state on the "
                             "rendered video)",
                        variable=self.debug
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        r += 1

        ttk.Label(grid, text="Text slides (one per line)"
                  ).grid(row=r, column=0, sticky="nw", pady=2)
        self.text_slides_widget = tk.Text(grid, height=4, wrap="word",
                                          font=("Menlo", 11))
        self.text_slides_widget.grid(row=r, column=1, columnspan=2,
                                     sticky="ew", padx=4)
        initial_text = _store.get(f"{self.persist_prefix}.text_slides", "")
        if initial_text:
            self.text_slides_widget.insert("1.0", initial_text)
        self.text_slides_widget.bind("<KeyRelease>", self._save_text_slides)
        self.text_slides_widget.bind("<FocusOut>", self._save_text_slides)
        r += 1

        ttk.Label(grid, text="Text-slide font (optional)"
                  ).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.text_slide_font
                  ).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="File...",
                   command=lambda: self._pick_font(self.text_slide_font)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid,
                  text="(16:9 picks landscape images, 9:16 picks portrait. "
                       "Title/end screens come from assets/templates/<aspect>/. "
                       "Video is written into the image folder; the current "
                       "project name is used as the output filename stem.)",
                  foreground="gray", wraplength=560, justify="left"
                  ).grid(row=r, column=0, columnspan=3, sticky="w")

        # The shared music field lives in the project bar; watch it so the
        # duration hint updates when the music (or project) changes.
        _make_global_var(_GLOBAL_MUSIC_KEY, _DEFAULT_MUSIC).trace_add(
            "write", lambda *_: self._refresh_duration_state())
        self._refresh_duration_state()

    def _refresh_duration_state(self):
        """If the music path points to a file with a sidecar JSON containing
        bar timestamps, grey out the duration Entry (image durations will
        come from the JSON) and show a hint. Otherwise re-enable it."""
        music = _current_music()
        bars_active = False
        if music and os.path.isfile(music):
            json_path = os.path.splitext(music)[0] + ".json"
            if os.path.isfile(json_path):
                try:
                    with open(json_path) as f:
                        data = json.load(f)
                    bars = data.get("bars") or []
                    bars_active = sum(1 for b in bars if "time" in b) >= 2
                except (OSError, ValueError):
                    bars_active = False
        if bars_active:
            self.duration_entry.state(["disabled"])
            self.duration_hint.configure(text="(using bar timings from sidecar JSON)")
        else:
            self.duration_entry.state(["!disabled"])
            self.duration_hint.configure(text="")

    def _save_text_slides(self, _event=None):
        value = self.text_slides_widget.get("1.0", "end-1c")
        _store.set(f"{self.persist_prefix}.text_slides", value)

    def _reload_extras(self):
        self.text_slides_widget.delete("1.0", "end")
        saved = _store.get(f"{self.persist_prefix}.text_slides", "")
        if saved:
            self.text_slides_widget.insert("1.0", saved)

    def _pick_font(self, var):
        current = var.get().strip()
        if current and os.path.isfile(current):
            initial_dir = os.path.dirname(current)
        elif sys.platform == "darwin":
            initial_dir = "/System/Library/Fonts"
        else:
            initial_dir = os.path.expanduser("~")
        path = filedialog.askopenfilename(
            initialdir=initial_dir,
            filetypes=[("Font files", "*.ttf *.ttc *.otf"),
                       ("All files", "*.*")],
            title="Pick a font file",
        )
        if path:
            var.set(path)

    def _gather_kwargs(self):
        folder = _current_image_folder()
        if not folder:
            raise ValueError("Pick an images folder in the project bar.")
        try:
            n = int(self.num_images.get())
        except ValueError:
            raise ValueError("Number of images must be an integer.")
        if n < 1:
            raise ValueError("Number of images must be >= 1.")
        try:
            dur = float(self.duration.get())
        except ValueError:
            raise ValueError("Duration per image must be a number.")
        if dur <= 0:
            raise ValueError("Duration per image must be > 0.")
        try:
            pct = float(self.kb_strength_pct.get())
        except ValueError:
            raise ValueError("Ken Burns strength must be a number.")
        if pct < 0 or pct > 100:
            raise ValueError("Ken Burns strength must be in 0..100.")

        kw = {
            "folder": folder,
            "num_images": n,
            "aspect": self.aspect.get(),
            "duration_per_image": dur,
            "kb_strength": pct / 100.0,
            "random_order": self.random_order.get(),
            "start_at_crop": self.start_at_crop.get(),
            "debug": self.debug.get(),
        }
        music = _current_music()
        if music:
            kw["music"] = music
        if self.gimmick.get():
            kw["gimmick"] = True
        tset = _current_template_set()
        end_screen = template_for(self.aspect.get(), "end-screen",
                                  template_set=tset)
        if end_screen:
            kw["end_screen"] = end_screen
        title_screen = template_for(self.aspect.get(), "title-screen",
                                    template_set=tset)
        if title_screen:
            kw["title_screen"] = title_screen
        title = _current_title()
        if title:
            kw["title"] = title
        subtitle = _current_subtitle()
        if subtitle:
            kw["subtitle"] = subtitle
        title_font = _current_title_font()
        if title_font:
            kw["title_font"] = title_font
        subtitle_font = _current_subtitle_font()
        if subtitle_font:
            kw["subtitle_font"] = subtitle_font
        if self.text_slide_font.get().strip():
            kw["text_slide_font"] = self.text_slide_font.get().strip()
        text_lines = [
            line.strip()
            for line in self.text_slides_widget.get("1.0", "end-1c").splitlines()
            if line.strip()
        ]
        if text_lines:
            kw["text_slides"] = text_lines
        kw["project_name"] = _store.current_project
        kw["output"] = self._default_output(folder)
        return kw


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_PROJECT_NAME_RE = re.compile(r"^[\w\- ]+$")


def _prompt_project_name(parent, title, prompt, initial=""):
    while True:
        name = simpledialog.askstring(
            title, prompt, parent=parent, initialvalue=initial)
        if name is None:
            return None
        name = name.strip()
        if not name:
            messagebox.showerror(title, "Name cannot be empty.", parent=parent)
            continue
        if not _PROJECT_NAME_RE.match(name):
            messagebox.showerror(
                title,
                "Use letters, digits, spaces, dashes and underscores only.",
                parent=parent)
            continue
        if name in _store.list_projects():
            messagebox.showerror(
                title, f"Project '{name}' already exists.", parent=parent)
            continue
        return name


def _change_images_folder(root):
    """Pick a new images folder; changing it always creates a new project.

    The user is prompted (and forced) to name the new project. On confirm, the
    current project's settings are cloned into the new project, which then owns
    the chosen folder. Returns the new project name, or None if the user
    cancelled either dialog (in which case the folder is left unchanged).
    """
    folder_var = _make_global_folder_var()
    path = filedialog.askdirectory(initialdir=folder_var.get() or PROJECT_ROOT)
    if not path:
        return None
    suggested = os.path.basename(os.path.normpath(path))
    name = _prompt_project_name(
        root, "New project",
        "Changing the images folder creates a new project.\n"
        "Enter a name for the new project:",
        initial=suggested)
    if name is None:
        return None  # cancelled — leave the current folder/project unchanged
    try:
        _store.clone_project(name)
    except ValueError as e:
        messagebox.showerror("New project", str(e), parent=root)
        return None
    folder_var.set(path)
    return name


def _build_project_bar(root):
    outer = ttk.Frame(root, padding=(8, 6, 8, 0))
    outer.pack(fill="x")

    # Row 1: project dropdown + clone/delete buttons.
    top = ttk.Frame(outer)
    top.pack(fill="x")
    ttk.Label(top, text="Project:").pack(side="left")

    current = tk.StringVar(value=_store.current_project)
    combo = ttk.Combobox(top, textvariable=current, state="readonly",
                         values=_store.list_projects(), width=28)
    combo.pack(side="left", padx=(6, 8))

    _syncing = {"v": False}  # guard so combo/store don't ping-pong

    def on_pick(_evt=None):
        if _syncing["v"]:
            return
        name = current.get()
        if name and name != _store.current_project:
            _store.switch_project(name)

    def on_store_change():
        _syncing["v"] = True
        try:
            combo.configure(values=_store.list_projects())
            current.set(_store.current_project)
        finally:
            _syncing["v"] = False

    _store.add_listener(on_store_change)
    combo.bind("<<ComboboxSelected>>", on_pick)

    def on_clone():
        name = _prompt_project_name(
            root, "Clone project",
            f"Clone '{_store.current_project}' to a new project named:")
        if name is None:
            return
        try:
            _store.clone_project(name)
        except ValueError as e:
            messagebox.showerror("Clone project", str(e), parent=root)

    def on_delete():
        target = _store.current_project
        if len(_store.list_projects()) <= 1:
            messagebox.showinfo(
                "Delete project",
                "Cannot delete the last remaining project.",
                parent=root)
            return
        if not messagebox.askyesno(
                "Delete project",
                f"Delete project '{target}'?\nThis cannot be undone.",
                parent=root):
            return
        _store.delete_project(target)

    ttk.Button(top, text="Clone...", command=on_clone).pack(side="left")
    ttk.Button(top, text="Delete", command=on_delete).pack(side="left",
                                                            padx=(6, 0))

    # Template set (subdirectory of assets/templates/) used for the title and
    # end screens. Per-project, chosen from a dropdown of available sets.
    template_var = _make_global_var(_GLOBAL_TEMPLATE_SET_KEY,
                                    _default_template_set())
    ttk.Label(top, text="Templates:").pack(side="left", padx=(16, 0))
    ttk.Combobox(top, textvariable=template_var, state="readonly",
                 values=list_template_sets(), width=18).pack(side="left",
                                                             padx=(6, 0))

    # Row 2: shared image folder for the current project. Changing the folder
    # always creates a new (user-named) project, so each image directory maps
    # to its own project.
    folder_var = _make_global_folder_var()
    row2 = ttk.Frame(outer)
    row2.pack(fill="x", pady=(6, 0))
    ttk.Label(row2, text="Images", width=8).pack(side="left")
    # Read-only so the only way to change the folder is via Browse..., which
    # routes through the forced new-project flow.
    entry = ttk.Entry(row2, textvariable=folder_var, state="readonly")
    entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
    ttk.Button(row2, text="Browse...",
               command=lambda: _change_images_folder(root)
               ).pack(side="left")

    # Status flag: a background watcher warns (in red) if the folder vanishes
    # or holds no images. Kept on `root` so it isn't garbage-collected.
    folder_status = ttk.Label(row2, text="")
    folder_status.pack(side="left", padx=(8, 0))
    root._folder_watcher = _FolderWatcher(root, folder_var, folder_status)

    # Row 3: shared music (file or folder) for the current project.
    music_var = _make_global_var(_GLOBAL_MUSIC_KEY, _DEFAULT_MUSIC)
    row3 = ttk.Frame(outer)
    row3.pack(fill="x", pady=(6, 0))
    ttk.Label(row3, text="Music", width=8).pack(side="left")
    ttk.Entry(row3, textvariable=music_var).pack(
        side="left", fill="x", expand=True, padx=(6, 6))
    ttk.Button(row3, text="File...",
               command=lambda: _pick_music_into(music_var, root)
               ).pack(side="left")
    ttk.Button(row3, text="Folder...",
               command=lambda: pick_dir(
                   music_var, music_var.get() or os.path.expanduser("~"))
               ).pack(side="left", padx=(4, 0))

    # Row 4: shared title text + font for the current project.
    title_var = _make_global_var(_GLOBAL_TITLE_KEY)
    title_font_var = _make_global_var(_GLOBAL_TITLE_FONT_KEY)
    row4 = ttk.Frame(outer)
    row4.pack(fill="x", pady=(6, 0))
    ttk.Label(row4, text="Title", width=8).pack(side="left")
    ttk.Entry(row4, textvariable=title_var).pack(
        side="left", fill="x", expand=True, padx=(6, 6))
    ttk.Label(row4, text="Font").pack(side="left")
    ttk.Entry(row4, textvariable=title_font_var, width=18).pack(
        side="left", padx=(4, 4))
    ttk.Button(row4, text="Font...",
               command=lambda: _pick_font_into(title_font_var, root)
               ).pack(side="left")

    # Row 5: shared subtitle text + font for the current project.
    subtitle_var = _make_global_var(_GLOBAL_SUBTITLE_KEY)
    subtitle_font_var = _make_global_var(_GLOBAL_SUBTITLE_FONT_KEY)
    row5 = ttk.Frame(outer)
    row5.pack(fill="x", pady=(6, 0))
    ttk.Label(row5, text="Subtitle", width=8).pack(side="left")
    ttk.Entry(row5, textvariable=subtitle_var).pack(
        side="left", fill="x", expand=True, padx=(6, 6))
    ttk.Label(row5, text="Font").pack(side="left")
    ttk.Entry(row5, textvariable=subtitle_font_var, width=18).pack(
        side="left", padx=(4, 4))
    ttk.Button(row5, text="Font...",
               command=lambda: _pick_font_into(subtitle_font_var, root)
               ).pack(side="left")


def main():
    root = tk.Tk()
    root.title("Markus Voelter Photography - Photo Tools")
    root.geometry("900x720")

    _build_project_bar(root)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=8)

    nb.add(CollageTab(nb),     text="Collage")
    nb.add(FilmstripTab(nb),   text="Film Strip")
    nb.add(RotateVideoTab(nb), text="Rotate Video")
    nb.add(CarouselTab(nb),    text="Insta Carousel")
    nb.add(ScrollVideoTab(nb), text="Scroll Video")
    nb.add(ShuffleRevealTab(nb), text="Fast Scroll")
    nb.add(ReelTab(nb),        text="Insta Reel")
    nb.add(KenBurnsTab(nb),    text="Ken Burns")
    nb.add(WallsTab(nb),       text="Walls")
    nb.add(SplitTab(nb),       text="Long Image Split")

    # Remember the selected tab per project: save on change, restore on the
    # current project and whenever the project switches. The guard stops the
    # programmatic restore from being written straight back.
    _syncing_tab = {"v": False}

    def _on_tab_changed(_evt=None):
        if not _syncing_tab["v"]:
            _save_selected_tab(nb)

    def _restore_tab_for_project():
        _syncing_tab["v"] = True
        try:
            _restore_selected_tab(nb)
        finally:
            _syncing_tab["v"] = False

    nb.bind("<<NotebookTabChanged>>", _on_tab_changed)
    _store.add_listener(_restore_tab_for_project)
    _restore_tab_for_project()

    root.update_idletasks()
    root.lift()
    root.attributes("-topmost", True)
    root.after(200, lambda: root.attributes("-topmost", False))
    root.focus_force()

    root.mainloop()
