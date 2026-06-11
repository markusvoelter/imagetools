"""Wrapper around createCrops.rb (Ruby script + MiniMagick)."""

import os
import shutil
import subprocess

from . import DEFAULT_WATERMARK, PACKAGE_DIR, RunContext


SCRIPT_PATH = os.path.join(PACKAGE_DIR, "createCrops.rb")


def run(*, folder, watermark=None, ctx=None):
    """Generate cropped variants for every image in `folder`.

    The Ruby script writes results to `_cropped_<ratio>/` subfolders inside
    `folder`. The watermark (used for 16:9 crops) is passed as an absolute
    path to the Ruby script. Defaults to assets/watermarks/watermark.png.

    Returns the absolute input folder so the UI can reveal it.
    """
    if ctx is None:
        ctx = RunContext()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"Not a directory: {folder}")

    if watermark is None:
        watermark = DEFAULT_WATERMARK
    watermark = os.path.abspath(watermark)
    if not os.path.isfile(watermark):
        raise FileNotFoundError(f"Watermark not found: {watermark}")

    if not os.path.isfile(SCRIPT_PATH):
        raise FileNotFoundError(f"createCrops.rb not found at {SCRIPT_PATH}")

    ruby = shutil.which("ruby")
    if not ruby:
        raise RuntimeError(
            "ruby not found on PATH. Install ruby and the mini_magick gem."
        )

    cmd = [ruby, SCRIPT_PATH, folder, watermark]
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
