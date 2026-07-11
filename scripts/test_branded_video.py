"""Part A test — ONE branded video, end to end, no Ava.

Generates a Pollinations background from a fixed brand-aligned prompt, then
renders the BrandedQuote composition (square) with a sample hook, saving the MP4
to renders/branded/. Watch the output to confirm the branding looks right.

Requires config.USE_REAL_REMOTION=True and the video/ project installed
(npm install in video/). First render downloads a headless Chromium (~113MB).

    python scripts/test_branded_video.py
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from integrations import pollinations, remotion_runner

BG_PROMPT = ("dark abstract neural network, cyan glowing nodes, pink accent "
             "lines, minimal, deep black background")
HOOK = "Agencies quote 6 months and $200K. We ship in 14 days for $8K."
FORMAT = "square"
OUT_DIR = os.path.join(config.RENDER_DIR, "branded")


def main():
    print("=" * 62)
    print("Branded video — Part A end-to-end test (no Ava)")
    print("=" * 62)
    print(f"  USE_POLLINATIONS   = {config.USE_POLLINATIONS}")
    print(f"  USE_REAL_REMOTION  = {config.USE_REAL_REMOTION}")
    if not config.USE_REAL_REMOTION:
        print("\n[!] USE_REAL_REMOTION is False — the render will STUB (empty MP4). "
              "Set USE_REAL_REMOTION=True in .env for a real render.")

    # 1) background via Pollinations
    print(f"\n[1/2] generating background: {BG_PROMPT!r}")
    t0 = time.time()
    bg = pollinations.generate_image(BG_PROMPT, width=1080, height=1080)
    if not bg:
        print("[x] background generation failed — aborting."); return
    print(f"      background -> {bg}  ({time.time()-t0:.1f}s)")

    # 2) render the branded video
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"branded_{int(time.time())}.mp4")
    print(f"\n[2/2] rendering BrandedQuote ({FORMAT}) — first render downloads "
          "Chromium, be patient...")
    t0 = time.time()
    result = remotion_runner.render_branded_video(
        hook_text=HOOK, background_path=bg, format=FORMAT,
        output_path=out_path, show_handle=False)
    dt = time.time() - t0

    print("\n" + "=" * 62)
    if result and os.path.exists(result) and os.path.getsize(result) > 0:
        print(f"OK — rendered in {dt:.1f}s:\n  {result}  "
              f"({os.path.getsize(result)/1024:.0f} KB)")
        print("\nOpen it and confirm the branding (ZentraIQ mark, tagline, URL, "
              "hook, colors) before we wire Ava (Part B).")
    elif result and os.path.getsize(result) == 0:
        print(f"STUB output (empty file) at {result} — set USE_REAL_REMOTION=True "
              "for a real render.")
    else:
        print(f"FAILED after {dt:.1f}s — see the [remotion] message above.")


if __name__ == "__main__":
    main()
