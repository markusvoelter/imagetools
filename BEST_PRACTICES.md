# Image Tools — Best Practices

Conventions distilled from the `image_tools` package (Tkinter UI + PIL/ffmpeg image and video generators). Drop this into another Claude session for context when building similar tools.

## Architecture

- One Python module per tool (`collage.py`, `reel.py`, `ken_burns.py`, …). Each exposes a single `run(*, ..., ctx=None)` keyword-only function. No global state, no CLI argparse boilerplate inside the tool — the UI calls `run(**kwargs)`.
- A thin `__init__.py` holds shared constants (`OUTPUT_DIR`, `WATERMARKS_DIR`, `DEFAULT_WATERMARK`, …), the `RunContext` helper, and the `RunCancelled` exception.
- A single `ui.py` builds a `ttk.Notebook` with one tab per tool. Each tab is a `RunnerTab` subclass that knows nothing about the tool internals beyond the kwargs to pass.

## `run()` signature

```python
def run(*, folder, ..., output=None, ctx=None):
    if ctx is None:
        ctx = RunContext()
    # validate args → raise ValueError with user-friendly message
    # do work, log via ctx.log(...), honor ctx.cancelled()
    # return the absolute output path (or None if cancelled)
```

- Keyword-only args. Defaults expressed as module-level constants (`DEFAULT_DURATION`, `FPS`, …) so they're discoverable.
- Validate inputs early; raise `ValueError("user-readable text")`. The UI surfaces the message verbatim.
- Auto-generate output path under `OUTPUT_DIR` with a timestamp when `output is None`. Otherwise `os.path.abspath` it and `os.makedirs(dirname, exist_ok=True)`.
- Always return the output path on success — the UI uses it to enable the "Reveal output" button.

## `RunContext` — logging, cancellation, child processes

```python
class RunContext:
    def __init__(self, log=None): ...
    def log(self, msg=""): ...           # callable from any thread
    def cancelled(self) -> bool: ...
    def check_cancelled(self): ...       # raises RunCancelled
    def register_process(self, proc): ...
    def cancel(self): ...                # sets event + terminates children
```

- Pass `ctx` into every long-running tool. Default to a stdout-logging `RunContext()` so the function is usable outside the UI.
- Long loops (frame rendering, file scans) must check `ctx.cancelled()` between iterations and break cleanly.
- When spawning ffmpeg or any subprocess, call `ctx.register_process(proc)` immediately so Stop can terminate it.

## Image processing (PIL)

- Standard image extensions set: `{'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}`. Audio: `{'.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus'}`.
- Use `Image.open(p)` inside `with` blocks when only reading metadata; convert with `.convert('RGB')` or `.convert('RGBA')` before pasting/blending.
- Resize with `Image.LANCZOS` for quality. Use `Image.BILINEAR` for per-frame affine sampling (speed).
- Cropping: pick `crop_fill` (center-crop to target aspect, then resize) vs `fit` (preserve full image, letterbox) per use case.
- For "fill the bars" effects, blur a scaled-to-fill copy of the same image and paste the fitted sharp version on top — never a flat color where you can avoid it.
- When scanning huge folders for matches, shuffle filenames first and bail out of the scan as soon as you've collected `target_count` matches. Don't open thousands of images unnecessarily.
- Log progress every ~100 files for long scans.

## Video generation (ffmpeg via pipe)

- Render frames in Python with PIL → feed raw RGB24 to ffmpeg via stdin. Pattern:

  ```python
  cmd = ['ffmpeg', '-y',
         '-f', 'rawvideo', '-vcodec', 'rawvideo',
         '-s', f'{W}x{H}', '-pix_fmt', 'rgb24', '-r', str(FPS),
         '-i', '-',
         # ...audio inputs / filters...
         '-vcodec', 'libx264', '-pix_fmt', 'yuv420p',
         '-preset', 'medium', '-crf', '18',
         output]
  proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
  ctx.register_process(proc)
  try:
      for frame in frames:
          if ctx.cancelled(): break
          proc.stdin.write(frame.tobytes())
  except BrokenPipeError:
      pass
  finally:
      proc.stdin.close()
      proc.wait()
  ```

- Output defaults: H.264 yuv420p, CRF 18, `medium` preset. AAC 192k for audio.
- Aspect presets live in a module-level dict, e.g. `ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920)}`.
- Standard FPS = 30.
- Crossfades: render in Python (`Image.blend(a, b, t)` per frame) when you need per-region control; use `-filter_complex` only when audio mixing requires it.
- Audio: support both a single file path and a folder (pick a random track inside). Always apply a tail fade (`afade=t=out:st=…:d=…`) so the music doesn't cut.
- Always include `-shortest` when audio is mapped, so the audio track doesn't extend the video.

## Tkinter UI (`RunnerTab` pattern)

- Subclass `RunnerTab`, set `persist_prefix = "<tool>"`, implement `_build_form()` and `_gather_kwargs()`.
- Inside `_build_form()`, create variables with `self.pvar(name, default, var_class=tk.StringVar)`. They auto-load from and save to `.image_tools_ui_state.json` keyed by `f"{persist_prefix}.{name}"`.
- `_gather_kwargs()` converts the string vars into typed kwargs and raises `ValueError(...)` with a user-readable message on bad input. Don't validate inside `_build_form()`.
- Layout convention: a `ttk.Frame` with `grid`; `columnconfigure(1, weight=1)`; entries in column 1, `Browse…` / `Pick color…` / `Save as…` buttons in column 2. Hint labels span all columns in gray.
- File pickers (`filedialog.askopenfilename`, `asksaveasfilename`, `askdirectory`) start from the current value's directory when set, otherwise from a sensible default (`OUTPUT_DIR`, `WATERMARKS_DIR`, `PROJECT_ROOT`, etc.).
- Color pickers use `tkinter.colorchooser.askcolor` seeded with the current hex value.

## Threading model

- The UI thread runs Tk's main loop. The tool runs in a `threading.Thread(daemon=True)`.
- Logs from the worker go through `queue.Queue`; the UI polls it every 80 ms via `self.after(80, self._drain_log)` — never call Tk methods from the worker thread.
- The worker pushes `None` into the queue when finished; the UI calls `_on_done()` to reset Run/Stop/Reveal button states.
- Stop button calls `ctx.cancel()`. The worker is responsible for checking `ctx.cancelled()` and exiting cleanly (or raising `RunCancelled`, which the wrapper catches and logs as `[cancelled]`).
- Wrap the worker's `run_fn` call in `try/except RunCancelled / except Exception` and always `log_queue.put(None)` in the `finally` so the UI un-disables Run even on crash.

## Persistent UI state

- `_PersistentStore` is a tiny JSON-backed dict at `.image_tools_ui_state.json` in cwd. Persists on every `.set()` (debounced only by equality check).
- `RunnerTab.pvar()` wires a `tk.Variable` to the store via `trace_add("write", ...)`. Survives across launches; per-tool prefix prevents key collisions.
- Coerce JSON-decoded values back to the var class' expected type before constructing (an int stored from a `StringVar` would come back as int and break Tk).

## File system conventions

- `OUTPUT_DIR = <project>/output` — call `ensure_output_dir()` before writing.
- Auto-generated filenames include the aspect ratio (`1920x1080` → `16x9`) and a `%Y%m%d_%H%M%S` timestamp.
- Cross-platform "reveal in file manager": macOS uses `open -R` (file) / `open` (dir); Linux uses `xdg-open` on the containing directory; Windows uses `explorer /select,`.

## Logging style

- One progress line per outer-loop iteration; group sub-progress with two-space indent.
- Use `ctx.log(f"[{i + 1}/{n}] {path.name}")` for per-item progress.
- Summary lines at the top of a run: input counts, output dimensions, total expected duration. Lets the user sanity-check before the long render starts.
- Errors raised as exceptions, not logged-and-returned. The UI wrapper formats them as `[error] …` + traceback.

## Things to avoid

- Don't hardcode absolute paths inside tool modules (the music folder default in `ui.py` is a pragmatic exception — it's a user-specific UI default, not a tool-level constant).
- Don't call `print` inside `run()`. Use `ctx.log`.
- Don't catch `Exception` inside the tool — let it propagate so the UI wrapper logs the traceback. Only catch what you can recover from (e.g. `Image.open` failing on a single bad file during a folder scan).
- Don't block the UI thread on subprocess waits — that's what the worker thread + queue is for.
- Don't write to `proc.stdin` after the cancellation flag is set; the subprocess is already being terminated and you'll get a `BrokenPipeError`.
