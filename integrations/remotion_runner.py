"""Remotion render wrapper.

Two entry points:
  render()               — the original generic stub Ava uses (unchanged).
  render_branded_video() — renders the branded BrandedQuote composition in the
                           video/ Remotion project, driven by props.

Both honor config.USE_REAL_REMOTION: when False they STUB (write an empty file
with the right name and log the command) so Ava's decision-making can be tested
without a real render. Real rendering shells out to `npx remotion render`.
Render failures (Node missing, project not installed, render error) are handled
gracefully — a clear one-line message, no traceback, and (for the branded path)
a None return so callers can fall back.
"""
import json
import os
import shutil
import subprocess
import time

import config

# The Remotion project lives at <project root>/video.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_DIR = os.path.join(_PROJECT_ROOT, "video")
PUBLIC_DIR = os.path.join(VIDEO_DIR, "public")
ENTRY = "src/index.ts"
COMPOSITION = "BrandedQuote"


def _npx():
    """npx executable name (npx.cmd on Windows so subprocess can find it)."""
    return "npx.cmd" if os.name == "nt" else "npx"


def render(composition, props, dest_path):
    """Render `composition` with `props` to `dest_path` (generic; used by Ava).

    Stub mode (default): writes an empty placeholder MP4 and logs the command.
    Real mode (config.USE_REAL_REMOTION): shells out to npx remotion render.
    Returns dest_path.
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    args = ["npx", "remotion", "render", composition, dest_path,
            "--props", json.dumps(props)]

    if not config.USE_REAL_REMOTION:
        with open(dest_path, "wb"):
            pass  # empty file — stands in for the rendered MP4
        print(f"[remotion:STUB] would run: {' '.join(args)}")
        print(f"[remotion:STUB] wrote placeholder MP4 -> {dest_path}")
        return dest_path

    subprocess.run(args, check=True)
    return dest_path


def render_branded_video(hook_text, background_path, format, output_path,
                         show_handle=False):
    """Render the branded BrandedQuote composition to `output_path`.

    Props: hookText, backgroundImagePath (a file in video/public/), format
    ("square"|"story"), showHandle. The Pollinations background at
    `background_path` is copied into video/public/ so staticFile() can resolve
    it; the copy and the temp props file are cleaned up afterward.

    Returns output_path on success, or None on failure (Node missing, project
    not installed, render error, missing background). Never raises for handled
    failures.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if not config.USE_REAL_REMOTION:
        with open(output_path, "wb"):
            pass
        print(f"[remotion:STUB] would render {COMPOSITION} "
              f"(format={format}, handle={show_handle}) -> {output_path}")
        print(f"[remotion:STUB] hook: {hook_text!r}")
        return output_path

    if not background_path or not os.path.exists(background_path):
        print(f"[remotion] background image not found: {background_path!r}")
        return None
    if not os.path.isdir(os.path.join(VIDEO_DIR, "node_modules")):
        print("[remotion] video/ project not installed — run `npm install` in "
              "the video/ folder first.")
        return None

    os.makedirs(PUBLIC_DIR, exist_ok=True)
    stamp = int(time.time() * 1000)
    ext = os.path.splitext(background_path)[1] or ".jpg"
    bg_name = f"bg-{stamp}{ext}"                 # basename passed as the prop
    bg_dest = os.path.join(PUBLIC_DIR, bg_name)
    props_file = os.path.join(VIDEO_DIR, f".render-props-{stamp}.json")

    try:
        shutil.copyfile(background_path, bg_dest)
        props = {
            "hookText": hook_text,
            "backgroundImagePath": bg_name,
            "format": format,
            "showHandle": bool(show_handle),
        }
        with open(props_file, "w", encoding="utf-8") as f:
            json.dump(props, f)

        # A props FILE (not inline JSON) avoids Windows command-line length and
        # quoting problems with arbitrary hook text.
        args = [_npx(), "remotion", "render", ENTRY, COMPOSITION,
                os.path.abspath(output_path), f"--props={props_file}"]
        print(f"[remotion] rendering {COMPOSITION} (format={format}) -> {output_path}")
        proc = subprocess.run(args, cwd=VIDEO_DIR, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            msg = tail[-1] if tail else f"exit code {proc.returncode}"
            print(f"[remotion] render failed: {msg}")
            return None
    except FileNotFoundError:
        print("[remotion] `npx` not found — is Node.js installed and on PATH?")
        return None
    except OSError as e:
        print(f"[remotion] render error: {type(e).__name__}: {e}")
        return None
    finally:
        for tmp in (bg_dest, props_file):
            try:
                os.remove(tmp)
            except OSError:
                pass

    if not os.path.exists(output_path):
        print("[remotion] render reported success but no output file was written.")
        return None
    print(f"[remotion] rendered -> {output_path}")
    return output_path
