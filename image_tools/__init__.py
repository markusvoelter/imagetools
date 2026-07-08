"""Image Tools — collage, video, carousel, and reel generators."""

import os
import threading


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
WATERMARKS_DIR = os.path.join(ASSETS_DIR, "watermarks")
WALLS_DIR = os.path.join(ASSETS_DIR, "walls")
TEMPLATES_DIR = os.path.join(ASSETS_DIR, "templates")
DEFAULT_WATERMARK = os.path.join(WATERMARKS_DIR, "watermark.png")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")


def template_for(aspect, kind, ext="jpg"):
    """Return the path to an aspect-specific template asset, or None if it
    does not exist.

    `aspect` is a ratio like "16:9" or "9:16"; the folder name uses "_"
    instead of ":". `kind` is a filename stem such as "title-screen" or
    "end-screen".
    """
    if not aspect:
        return None
    folder = aspect.replace(":", "_")
    path = os.path.join(TEMPLATES_DIR, folder, f"{kind}.{ext}")
    return path if os.path.isfile(path) else None


class RunCancelled(Exception):
    """Raised by a tool when the user has requested cancellation."""


class RunContext:
    """Per-run context passed into each tool's run() function.

    Carries a log callback, cancellation flag, and a list of child processes
    so the UI can terminate ffmpeg or similar when the user clicks Stop.
    """

    def __init__(self, log=None):
        self._log_fn = log if log is not None else (lambda msg: print(msg))
        self._cancel = threading.Event()
        self._processes = []

    def log(self, msg=""):
        self._log_fn(str(msg))

    def cancelled(self):
        return self._cancel.is_set()

    def check_cancelled(self):
        if self._cancel.is_set():
            raise RunCancelled()

    def register_process(self, proc):
        self._processes.append(proc)

    def cancel(self):
        self._cancel.set()
        for p in self._processes:
            try:
                p.terminate()
            except Exception:
                pass


def ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return OUTPUT_DIR
