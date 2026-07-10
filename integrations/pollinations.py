"""Pollinations.ai image generation — free, no API key.

generate_image(prompt, ...) hits Pollinations' text-to-image endpoint
(https://image.pollinations.ai/prompt/<url-encoded-prompt>), saves the returned
PNG under RENDER_DIR/generated/, and returns the path. On any failure it prints
one clear line (no traceback) and returns None so the caller can fall back.

Stage 1: standalone only — NOT wired into Ava yet.
"""
import os
import time
from urllib.parse import quote

import requests

import config

# Their image endpoint takes the prompt as a path segment, plus query params.
_BASE_URL = "https://image.pollinations.ai/prompt/"
# Generated images live alongside the Remotion renders, in their own subfolder.
GENERATED_DIR = os.path.join(config.RENDER_DIR, "generated")
# A generous timeout — Pollinations renders on demand and can take a while.
_TIMEOUT = 120

# Pollinations serves JPEG (despite the /prompt/ path), so name the saved file by
# its real Content-Type rather than assuming .png.
_EXT_BY_TYPE = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                "image/webp": ".webp", "image/gif": ".gif"}


def _ext_for(content_type):
    base = content_type.split(";", 1)[0].strip().lower()
    if base in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[base]
    # Fallback: derive from the subtype (image/foo -> .foo), else .img.
    return f".{base.split('/', 1)[1]}" if base.startswith("image/") else ".img"


def generate_image(prompt, width=1024, height=1024, seed=None, nologo=True):
    """Generate an image from `prompt` and save it locally.

    Returns the saved file path (str) on success, or None on failure (network
    error, non-image/empty response, disabled via config). Never raises for
    handled failures — the caller checks for None and falls back.

    Note: the free tier honors the requested ASPECT RATIO but caps resolution
    (~768 for square, ~1024 on the long side) — e.g. width/height 1080x1920 comes
    back 576x1024. Stage 2 should upscale in the Remotion pipeline if it needs
    full-res. Output is JPEG despite the /prompt/ path; the file is named by its
    real Content-Type.
    """
    if not config.USE_POLLINATIONS:
        print("[pollinations] disabled (USE_POLLINATIONS=False) — skipping.")
        return None
    if not prompt or not prompt.strip():
        print("[pollinations] empty prompt — nothing to generate.")
        return None

    params = {"width": width, "height": height}
    if seed is not None:
        params["seed"] = seed
    if nologo:
        params["nologo"] = "true"
    url = _BASE_URL + quote(prompt.strip(), safe="")

    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"[pollinations] timed out after {_TIMEOUT}s — no image.")
        return None
    except requests.exceptions.RequestException as e:
        print(f"[pollinations] request failed: {type(e).__name__}.")
        return None

    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/") or not resp.content:
        print(f"[pollinations] non-image response (Content-Type={content_type!r}, "
              f"{len(resp.content)} bytes) — no image.")
        return None

    try:
        os.makedirs(GENERATED_DIR, exist_ok=True)
        path = os.path.join(GENERATED_DIR,
                            f"{int(time.time() * 1000)}{_ext_for(content_type)}")
        with open(path, "wb") as f:
            f.write(resp.content)
    except OSError as e:
        print(f"[pollinations] could not save image: {type(e).__name__}.")
        return None

    print(f"[pollinations] saved {len(resp.content)} bytes -> {path}")
    return path
