"""Tkinter UI for the image-tools package."""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import colorchooser, filedialog, ttk

from . import (
    DEFAULT_WATERMARK,
    OUTPUT_DIR,
    PROJECT_ROOT,
    RunCancelled,
    RunContext,
    WALLS_DIR,
    WATERMARKS_DIR,
    ensure_output_dir,
)
from . import carousel as carousel_mod
from . import collage as collage_mod
from . import cropping as cropping_mod
from . import ken_burns as ken_burns_mod
from . import reel as reel_mod
from . import rotate_video as rotate_video_mod
from . import scroll_video as scroll_video_mod
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


class _PersistentStore:
    """JSON-backed key/value store. Persisted automatically on each set()."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(self.path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data = loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def get(self, key, default):
        return self.data.get(key, default)

    def set(self, key, value):
        if self.data.get(key) == value:
            return
        self.data[key] = value
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
        except OSError:
            pass


_store = _PersistentStore(_STATE_PATH)


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

    def __init__(self, master, run_fn, default_input_dir=None):
        super().__init__(master, padding=10)
        self.run_fn = run_fn
        self.default_input_dir = default_input_dir
        self.ctx = None
        self.worker = None
        self.log_queue = queue.Queue()
        self.last_output = None
        self._build_form()
        self._build_log()
        self.after(80, self._drain_log)

    def pvar(self, name, default, var_class=tk.StringVar):
        """Return a tk.Variable bound to the global JSON store.

        The store key is f"{self.persist_prefix}.{name}". If `persist_prefix`
        is None (e.g. for a tab that opts out), the variable is created with
        the supplied default and no persistence wiring.
        """
        if self.persist_prefix is None:
            return var_class(value=default)
        key = f"{self.persist_prefix}.{name}"
        stored = _store.get(key, default)
        # Coerce JSON-decoded values back to the var class' expected type so
        # tk doesn't choke on e.g. a stored int going into a StringVar.
        if var_class is tk.StringVar and not isinstance(stored, str):
            stored = "" if stored is None else str(stored)
        elif var_class is tk.BooleanVar and not isinstance(stored, bool):
            stored = bool(stored)
        var = var_class(value=stored)
        var.trace_add("write", lambda *a: _store.set(key, var.get()))
        return var

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

    def __init__(self, master):
        super().__init__(master, collage_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imageCollage"))

    def _build_form(self):
        self.folder = self.pvar("folder", "")
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
        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

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
        if not self.folder.get().strip():
            raise ValueError("Pick an image folder.")
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
            "folder": self.folder.get().strip(),
            "style": self.style.get(),
        }
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
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
# Rotate Video
# ---------------------------------------------------------------------------

class RotateVideoTab(RunnerTab):
    persist_prefix = "rotate_video"

    def __init__(self, master):
        super().__init__(master, rotate_video_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imageRotateVideo"))

    def _build_form(self):
        self.folder = self.pvar("folder", "")
        self.duration = self.pvar("duration", "15")
        self.cover = self.pvar("cover", "")
        self.music = self.pvar(
            "music",
            "/Users/markusvoelter/Documents/projects/photo.voelter.de/media/ai-music",
        )
        self.output = self.pvar("output", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Total duration (seconds)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.duration, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Cover image").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.cover).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...", command=self._pick_cover
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Music file or folder (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.music).grid(row=r, column=1, sticky="ew", padx=4)
        music_btns = ttk.Frame(grid)
        music_btns.grid(row=r, column=2, sticky="w")
        ttk.Button(music_btns, text="File...",
                   command=self._pick_music_file).pack(side="left")
        ttk.Button(music_btns, text="Folder...",
                   command=lambda: pick_dir(
                       self.music,
                       self.music.get() or os.path.expanduser("~"))
                   ).pack(side="left", padx=(4, 0))
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="(cover must be inside the chosen image folder)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _pick_music_file(self):
        current = self.music.get().strip()
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
            title="Pick an audio file",
        )
        if path:
            self.music.set(path)

    def _pick_cover(self):
        initial = self.folder.get() or self.default_input_dir
        path = filedialog.askopenfilename(
            initialdir=initial,
            filetypes=[("Image", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
        )
        if not path:
            return
        folder = self.folder.get()
        if folder and os.path.dirname(os.path.abspath(path)) == os.path.abspath(folder):
            self.cover.set(os.path.basename(path))
        else:
            # If folder hasn't been set yet, set it from the picked file
            if not folder:
                self.folder.set(os.path.dirname(path))
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
        if not self.folder.get().strip():
            raise ValueError("Pick an image folder.")
        if not self.duration.get().strip():
            raise ValueError("Enter a duration.")
        try:
            duration = float(self.duration.get())
        except ValueError:
            raise ValueError(f"Duration must be a number, got: {self.duration.get()}")
        if not self.cover.get().strip():
            raise ValueError("Pick the cover image.")

        kw = {
            "folder": self.folder.get().strip(),
            "total_duration_seconds": duration,
            "cover_image": self.cover.get().strip(),
        }
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        if self.music.get().strip():
            kw["music"] = self.music.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Carousel
# ---------------------------------------------------------------------------

class CarouselTab(RunnerTab):
    persist_prefix = "carousel"

    def __init__(self, master):
        super().__init__(master, carousel_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imagesSwipeys2"))

    def _build_form(self):
        self.folder = self.pvar("folder", "")
        self.num_slides = self.pvar("num_slides", "20")
        self.aspect = self.pvar("aspect", "9:16")
        self.output_dir = self.pvar("output_dir", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Number of slides").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_slides, width=10).grid(row=r, column=1, sticky="w", padx=4)
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
        if not self.folder.get().strip():
            raise ValueError("Pick an image folder.")
        if not self.num_slides.get().strip():
            raise ValueError("Enter the number of slides.")
        try:
            n = int(self.num_slides.get())
        except ValueError:
            raise ValueError("Number of slides must be an integer.")
        kw = {
            "folder": self.folder.get().strip(),
            "num_slides": n,
            "aspect_ratio": self.aspect.get(),
        }
        if self.output_dir.get().strip():
            kw["output_dir"] = self.output_dir.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Scroll Video
# ---------------------------------------------------------------------------

class ScrollVideoTab(RunnerTab):
    persist_prefix = "scroll_video"

    def __init__(self, master):
        super().__init__(master, scroll_video_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "imagesSwipeys2"))

    def _build_form(self):
        self.folder = self.pvar("folder", "")
        self.aspect = self.pvar("aspect", "9:16")
        self.output = self.pvar("output", "")
        self.scroll_mode = self.pvar("scroll_mode", "Continuous pan")
        self.stepped_hold_s = self.pvar("stepped_hold_s", "2.0")
        self.scroll_speed_pct = self.pvar("scroll_speed_pct", "200")
        self.music = self.pvar(
            "music",
            "/Users/markusvoelter/Documents/projects/photo.voelter.de/media/ai-music",
        )
        self.end_screen = self.pvar("end_screen", DEFAULT_WATERMARK)

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

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

        ttk.Label(grid, text="Music file or folder (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.music).grid(row=r, column=1, sticky="ew", padx=4)
        music_btns = ttk.Frame(grid)
        music_btns.grid(row=r, column=2, sticky="w")
        ttk.Button(music_btns, text="File...",
                   command=self._pick_music_file).pack(side="left")
        ttk.Button(music_btns, text="Folder...",
                   command=lambda: pick_dir(
                       self.music,
                       self.music.get() or os.path.expanduser("~"))
                   ).pack(side="left", padx=(4, 0))
        r += 1

        ttk.Label(grid, text="End screen image (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.end_screen).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="File...", command=self._pick_end_screen
                   ).grid(row=r, column=2)
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

    def _pick_end_screen(self):
        current = self.end_screen.get().strip()
        if current and os.path.isfile(current):
            initial_dir = os.path.dirname(current)
        else:
            initial_dir = WATERMARKS_DIR
        path = filedialog.askopenfilename(
            initialdir=initial_dir,
            filetypes=[("Image", "*.png *.jpg *.jpeg"), ("All files", "*.*")],
            title="Pick an end screen image",
        )
        if path:
            self.end_screen.set(path)

    def _pick_music_file(self):
        current = self.music.get().strip()
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
            title="Pick an audio file",
        )
        if path:
            self.music.set(path)

    def _gather_kwargs(self):
        if not self.folder.get().strip():
            raise ValueError("Pick an image folder.")
        mode_map = {
            "Continuous pan": scroll_video_mod.MODE_CONTINUOUS,
            "Hold each slide": scroll_video_mod.MODE_STEPPED,
        }
        kw = {
            "folder": self.folder.get().strip(),
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
        if self.music.get().strip():
            kw["music"] = self.music.get().strip()
        if self.end_screen.get().strip():
            kw["end_screen"] = self.end_screen.get().strip()
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Reel
# ---------------------------------------------------------------------------

class ReelTab(RunnerTab):
    persist_prefix = "reel"

    def __init__(self, master):
        super().__init__(master, reel_mod.run,
                         default_input_dir=os.path.join(PROJECT_ROOT, "vertHorizVideo"))

    def _build_form(self):
        self.folder = self.pvar("folder", "")
        self.interval = self.pvar("interval", "2.0")
        self.music_folder = self.pvar(
            "music_folder",
            "/Users/markusvoelter/Documents/projects/photo.voelter.de/media/ai-music",
        )
        self.beats_per_transition = self.pvar("beats_per_transition", "4")
        self.end_screen = self.pvar("end_screen", DEFAULT_WATERMARK)
        self.output = self.pvar("output", "")
        self.bg = self.pvar("bg", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Interval per image (seconds)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.interval, width=10).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Music file or folder (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.music_folder).grid(row=r, column=1, sticky="ew", padx=4)
        music_btns = ttk.Frame(grid)
        music_btns.grid(row=r, column=2, sticky="w")
        ttk.Button(music_btns, text="File...",
                   command=self._pick_music_file).pack(side="left")
        ttk.Button(music_btns, text="Folder...",
                   command=lambda: pick_dir(
                       self.music_folder,
                       self.music_folder.get() or os.path.expanduser("~"))
                   ).pack(side="left", padx=(4, 0))
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

        ttk.Label(grid, text="End screen image (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.end_screen).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="File...", command=self._pick_end_screen
                   ).grid(row=r, column=2)
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

    def _pick_end_screen(self):
        current = self.end_screen.get().strip()
        if current and os.path.isfile(current):
            initial_dir = os.path.dirname(current)
        else:
            initial_dir = os.path.expanduser("~")
        path = filedialog.askopenfilename(
            initialdir=initial_dir,
            filetypes=[("Image", "*.png *.jpg *.jpeg"), ("All files", "*.*")],
            title="Pick an end screen image",
        )
        if path:
            self.end_screen.set(path)

    def _pick_music_file(self):
        current = self.music_folder.get().strip()
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
            title="Pick an audio file",
        )
        if path:
            self.music_folder.set(path)

    def _gather_kwargs(self):
        if not self.folder.get().strip():
            raise ValueError("Pick an image folder.")
        kw = {"folder": self.folder.get().strip()}
        if self.interval.get().strip():
            try:
                kw["interval"] = float(self.interval.get())
            except ValueError:
                raise ValueError(f"Interval must be a number.")
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        if self.bg.get().strip():
            kw["bg"] = self.bg.get().strip()
        if self.music_folder.get().strip():
            kw["music"] = self.music_folder.get().strip()
        if self.end_screen.get().strip():
            kw["end_screen"] = self.end_screen.get().strip()
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
# Cropping (Ruby)
# ---------------------------------------------------------------------------

class CroppingTab(RunnerTab):
    persist_prefix = "cropping"

    def __init__(self, master):
        super().__init__(master, cropping_mod.run,
                         default_input_dir=PROJECT_ROOT)

    def _build_form(self):
        self.folder = self.pvar("folder", "")
        self.watermark = self.pvar("watermark", DEFAULT_WATERMARK)

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Watermark file").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.watermark).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...", command=self._pick_watermark
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid,
                  text="(crops are written to _cropped_<ratio>/ subfolders inside the input folder)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _pick_watermark(self):
        initial = self.watermark.get() or DEFAULT_WATERMARK
        path = filedialog.askopenfilename(
            initialdir=os.path.dirname(initial) if initial else WATERMARKS_DIR,
            initialfile=os.path.basename(initial) if initial else "",
            filetypes=[("PNG", "*.png"), ("Image", "*.jpg *.jpeg *.png"),
                       ("All files", "*.*")],
            title="Pick a watermark image",
        )
        if path:
            self.watermark.set(path)

    def _gather_kwargs(self):
        if not self.folder.get().strip():
            raise ValueError("Pick an image folder.")
        kw = {"folder": self.folder.get().strip()}
        if self.watermark.get().strip():
            kw["watermark"] = self.watermark.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Walls
# ---------------------------------------------------------------------------

class WallsTab(RunnerTab):
    persist_prefix = "walls"

    def __init__(self, master):
        super().__init__(master, walls_mod.run,
                         default_input_dir=PROJECT_ROOT)

    def _build_form(self):
        self.wall_folder = self.pvar("wall_folder", WALLS_DIR)
        self.image_folder = self.pvar("image_folder", "")
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

        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.image_folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.image_folder, self.default_input_dir)
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
        if not self.wall_folder.get().strip():
            raise ValueError("Pick a wall folder.")
        if not self.image_folder.get().strip():
            raise ValueError("Pick an image folder.")
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
            "image_folder": self.image_folder.get().strip(),
            "num_outputs": n,
        }
        if self.output_dir.get().strip():
            kw["output_dir"] = self.output_dir.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

class SplitTab(RunnerTab):
    persist_prefix = "split"

    def __init__(self, master):
        super().__init__(master, split_mod.run,
                         default_input_dir=PROJECT_ROOT)

    def _build_form(self):
        self.image = self.pvar("image", "")
        self.aspect_ratio = self.pvar("aspect_ratio", "9:16")
        self.output_dir = self.pvar("output_dir", "")
        self.bg = self.pvar("bg", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Input image").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.image).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...", command=self._pick_input).grid(row=r, column=2)
        r += 1

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
                  text="(slides keep original height; width = height × aspect ratio. "
                       "Padding is only used if input is narrower than one slide.)",
                  foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w")

    def _pick_input(self):
        current = self.image.get().strip()
        if current and os.path.isfile(current):
            initial_dir = os.path.dirname(current)
        else:
            initial_dir = current if current else self.default_input_dir
        path = filedialog.askopenfilename(
            initialdir=initial_dir,
            filetypes=[("Image", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
            title="Pick a landscape photo",
        )
        if path:
            self.image.set(path)

    def _gather_kwargs(self):
        if not self.image.get().strip():
            raise ValueError("Pick an input image.")
        kw = {
            "image": self.image.get().strip(),
            "aspect_ratio": self.aspect_ratio.get(),
        }
        if self.output_dir.get().strip():
            kw["output_dir"] = self.output_dir.get().strip()
        if self.bg.get().strip():
            kw["bg"] = self.bg.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Ken Burns
# ---------------------------------------------------------------------------

class KenBurnsTab(RunnerTab):
    persist_prefix = "ken_burns"

    def __init__(self, master):
        super().__init__(master, ken_burns_mod.run,
                         default_input_dir=PROJECT_ROOT)

    def _build_form(self):
        self.folder = self.pvar("folder", "")
        self.num_images = self.pvar("num_images", "20")
        self.aspect = self.pvar("aspect", "16:9")
        self.duration = self.pvar("duration", "4.0")
        self.kb_strength_pct = self.pvar("kb_strength_pct", "50")
        self.music = self.pvar(
            "music",
            "/Users/markusvoelter/Documents/projects/photo.voelter.de/media/ai-music",
        )
        self.output = self.pvar("output", "")

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(grid, text="Image folder").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.folder).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Browse...",
                   command=lambda: pick_dir(self.folder, self.default_input_dir)
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid, text="Number of images").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.num_images, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Aspect ratio").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.aspect,
                     values=list(ken_burns_mod.ASPECTS.keys()),
                     state="readonly").grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Duration per image (s)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.duration, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Ken Burns strength %").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.kb_strength_pct, width=10
                  ).grid(row=r, column=1, sticky="w", padx=4)
        r += 1

        ttk.Label(grid, text="Music file or folder (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.music).grid(row=r, column=1, sticky="ew", padx=4)
        music_btns = ttk.Frame(grid)
        music_btns.grid(row=r, column=2, sticky="w")
        ttk.Button(music_btns, text="File...",
                   command=self._pick_music_file).pack(side="left")
        ttk.Button(music_btns, text="Folder...",
                   command=lambda: pick_dir(
                       self.music,
                       self.music.get() or os.path.expanduser("~"))
                   ).pack(side="left", padx=(4, 0))
        r += 1

        ttk.Label(grid, text="Output file (optional)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.output).grid(row=r, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="Save as...", command=self._pick_output
                   ).grid(row=r, column=2)
        r += 1

        ttk.Label(grid,
                  text="(16:9 picks landscape images, 9:16 picks portrait. "
                       "Bars are filled with a heavy blur of the same image.)",
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

    def _pick_music_file(self):
        current = self.music.get().strip()
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
            title="Pick an audio file",
        )
        if path:
            self.music.set(path)

    def _gather_kwargs(self):
        if not self.folder.get().strip():
            raise ValueError("Pick an image folder.")
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
            "folder": self.folder.get().strip(),
            "num_images": n,
            "aspect": self.aspect.get(),
            "duration_per_image": dur,
            "kb_strength": pct / 100.0,
        }
        if self.music.get().strip():
            kw["music"] = self.music.get().strip()
        if self.output.get().strip():
            kw["output"] = self.output.get().strip()
        return kw


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    root.title("Markus Voelter Photography - Photo Tools")
    root.geometry("900x680")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=8)

    nb.add(CollageTab(nb),     text="Collage")
    nb.add(RotateVideoTab(nb), text="Rotate Video")
    nb.add(CarouselTab(nb),    text="Insta Carousel")
    nb.add(ScrollVideoTab(nb), text="Scroll Video")
    nb.add(ReelTab(nb),        text="Insta Reel")
    nb.add(KenBurnsTab(nb),    text="Ken Burns")
    nb.add(CroppingTab(nb),    text="Cropping")
    nb.add(WallsTab(nb),       text="Walls")
    nb.add(SplitTab(nb),       text="Long Image Split")

    root.update_idletasks()
    root.lift()
    root.attributes("-topmost", True)
    root.after(200, lambda: root.attributes("-topmost", False))
    root.focus_force()

    root.mainloop()
