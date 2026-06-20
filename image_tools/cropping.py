"""Wrapper around createCrops.rb (Ruby script + MiniMagick)."""

import os
import shutil
import subprocess

from . import PACKAGE_DIR, RunContext


SCRIPT_PATH = os.path.join(PACKAGE_DIR, "createCrops.rb")


def run(*, folder, watermark=None, ctx=None):
    """Generate cropped variants for every image in `folder`.

    The Ruby script writes results to `_cropped_<ratio>/` subfolders inside
    `folder`. If `watermark` is a path it is passed to the Ruby script and used
    on 16:9 crops; if it's None or an empty string, no watermark is applied
    (the 16:9 outputs are produced without one).

    Returns the absolute input folder so the UI can reveal it.
    """
    if ctx is None:
        ctx = RunContext()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    if watermark is not None and watermark.strip():
        watermark = os.path.abspath(watermark)
        if not os.path.isfile(watermark):
            raise FileNotFoundError(f"Watermark not found: {watermark}")
        ctx.log(f"Watermark: {watermark}")
    else:
        watermark = None
        ctx.log("No watermark — 16:9 crops will be produced without one.")

    if not os.path.isfile(SCRIPT_PATH):
        raise FileNotFoundError(f"createCrops.rb not found at {SCRIPT_PATH}")

    ruby = shutil.which("ruby")
    if not ruby:
        raise RuntimeError(
            "ruby not found on PATH. Install ruby and the mini_magick gem."
        )

    cmd = [ruby, SCRIPT_PATH, folder, watermark if watermark else ""]
    ctx.log(f"$ cd {PACKAGE_DIR}")
    ctx.log(f"$ {' '.join(cmd)}\n")

    proc = subprocess.Popen(
        cmd, cwd=PACKAGE_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    ctx.register_process(proc)
    for line in proc.stdout:
        ctx.log(line.rstrip("\n"))
    rc = proc.wait()

    if ctx.cancelled():
        ctx.log("Cancelled.")
        return None
    if rc != 0:
        raise RuntimeError(f"createCrops.rb exited with code {rc}")

    ctx.log(f"\nDone. Crops in _cropped_*/ subfolders of {folder}")
    return folder
