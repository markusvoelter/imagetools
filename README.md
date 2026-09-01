# Image Tools

A desktop toolkit for turning a folder of photos into social-media-ready
images and videos — collages, Instagram carousels, and several styles of
slideshow video (Ken Burns, reels, scrolling panoramas, and more).

Everything is driven from a single Tkinter GUI, but each tool is also a plain
Python function you can call directly.

## What it does

Point a tool at a folder of images and it produces one of the following. Image
tools write JP/PNG files; video tools shell out to **ffmpeg** to render MP4s.

| Tool | Output | Summary |
|------|--------|---------|
| **Collage** (`collage.py`) | JPEG | One collage in four layout styles: `grid` (justified rows), `columns` (justified columns), `mosaic` (tiled grid of mixed-size cells), and `creative` (scattered, rotated, drop-shadowed). |
| **Split** (`split.py`) | JPEGs | Slices a wide panorama photo into non-overlapping Instagram carousel slides at a chosen aspect ratio, padding the last slide and appending a full-image overview slide. |
| **Carousel** (`carousel.py`) | JPEGs | Assembles a horizontal panorama from many photos, then cuts it into carousel slides (with a "SWIPE" hint and per-slide EXIF capture times). |
| **Film strip** (`filmstrip.py`) | JPEG | Arranges photos as frames on tilted vertical film strips (sprocket holes, drop shadows) across a 9:16 canvas. |
| **Walls** (`walls.py`) | PNGs | Detects the empty black rectangle in a "wall scaffold" image and composites a well-matched photo into it. |
| **Ken Burns** (`ken_burns.py`) | MP4 | Slow pan/zoom slideshow (16:9 or 9:16) with optional music, beat-timed bars, title/text slides, and end screen. |
| **Reel** (`reel.py`) | MP4 | Instagram-style 9:16 slideshow with crossfades, optional music (beat-snapped transitions), and an optional end screen. |
| **Scroll video** (`scroll_video.py`) | MP4 | Pans across a panorama strip of images — either a continuous linear pan or a stepped hold-and-scroll. |
| **Shuffle reveal** (`shuffle_reveal.py`) | MP4 | Fast "whip" transitions that flash random images between each real reveal, optionally bar-synced to music. |
| **Rotate video** (`rotate_video.py`) | MP4 | Builds a 9:16 portrait video from a cover image plus horizontal photos. |

Audio-driven timing (Ken Burns / reel beat detection) uses **librosa** when
available and falls back to fixed-interval timing when it isn't.

## Requirements

- **Python 3** with Tkinter (the GUI toolkit).
- **ffmpeg** on your `PATH` — required by all the video tools (image-only tools
  work without it).
- Python packages from `requirements.txt`: `pillow`, `numpy`, `scipy`,
  `piexif`, and (optional) `librosa`.

## Running

The `run.sh` script bootstraps a virtualenv, installs dependencies, checks for
Tkinter/ffmpeg, and launches the GUI:

```bash
./run.sh
```

Use a specific interpreter to build the venv with `PYTHON=python3.12 ./run.sh`.
To force a dependency refresh, delete `.venv/.deps-installed` (or the whole
`.venv`).

### Calling a tool directly

Each tool exposes a keyword-only `run(...)` function. For example:

```python
from image_tools import collage, RunContext

collage.run(
    folder="/path/to/photos",
    style="mosaic",
    aspect="16:9",
    output="/path/to/out.jpg",
    ctx=RunContext(log=print),   # optional: receives progress logs
)
```

The random-driven tools (`collage`, `filmstrip`, `walls`, `carousel`,
`shuffle_reveal`) accept an optional `seed=` argument for reproducible output.

## Project layout

```
image_tools/           # the package: one module per tool + ui.py + shared helpers
  __init__.py          # RunContext (logging/cancellation), asset paths, output dir
  _panorama.py         # shared panorama-strip builder (carousel + scroll_video)
  ui.py                # Tkinter GUI wiring every tool to widgets
image_tools_ui.py      # GUI launcher entry point
assets/                # watermarks, wall scaffolds, and 16:9 / 9:16 title/end templates
output/                # default destination for generated files
run.sh                 # set up venv + deps, then launch the GUI
test.sh                # set up venv + test deps, then run the test suite
tests/                 # pytest suite (unit / image / video tiers)
```

`RunContext` (in `image_tools/__init__.py`) carries a log callback, a
cooperative cancellation flag, and a registry of child ffmpeg processes so the
GUI can stop a running job.

## Testing

A pytest suite covers the pure layout/geometry logic, image-producing tools,
and the video tools (with ffmpeg mocked for fast argv checks, plus
ffmpeg-gated end-to-end smoke tests).

```bash
./test.sh                          # everything
./test.sh -m "not slow"            # skip the real-ffmpeg render tests
./test.sh -m "not slow and not librosa"   # leanest, no heavy optional deps
```

Test inputs are generated at runtime (no committed binaries). Markers:
`slow` (real render), `ffmpeg` (needs the ffmpeg/ffprobe binaries), and
`librosa` (needs the optional audio dependency); the last two auto-skip when
the dependency is missing.
