"""Stage 1 test — generate a few Pollinations images to eyeball quality.

Generates 3 images from sample prompts and saves them under
renders/generated/, printing each saved path. No API key needed. Run this,
then open the PNGs to judge baseline quality before wiring into Ava (Stage 2).

    python scripts/test_pollinations.py
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
from integrations import pollinations

# Baseline prompts — a couple lean toward ZentraIQ's visual identity (dark,
# technical, cyan, minimal) so quality can be judged against the real use case,
# but the prompt shaping proper happens in Stage 2.
PROMPTS = [
    ("abstract AI neural network, dark background, cyan accents, minimal", None),
    ("clean isometric illustration of a SaaS dashboard, dark UI, teal and violet "
     "accents, technical, minimal, no text", 42),
    ("developer workspace at night, code on screen, moody lighting, cinematic, "
     "technical aesthetic", 7),
]


def main():
    print("=" * 60)
    print("Pollinations.ai — Stage 1 quality check")
    print("=" * 60)
    print(f"  USE_POLLINATIONS = {config.USE_POLLINATIONS}")
    print(f"  output dir       = {pollinations.GENERATED_DIR}\n")

    if not config.USE_POLLINATIONS:
        print("[x] USE_POLLINATIONS is False — enable it to generate images.")
        return

    saved = []
    for i, (prompt, seed) in enumerate(PROMPTS, 1):
        print(f"[{i}/{len(PROMPTS)}] prompt: {prompt!r}"
              + (f" (seed={seed})" if seed is not None else ""))
        t0 = time.time()
        path = pollinations.generate_image(prompt, width=1024, height=1024, seed=seed)
        dt = time.time() - t0
        if path and os.path.exists(path):
            size_kb = os.path.getsize(path) / 1024
            print(f"        -> {path}  ({size_kb:.0f} KB, {dt:.1f}s)\n")
            saved.append(path)
        else:
            print(f"        -> FAILED after {dt:.1f}s (returned {path!r})\n")

    print("=" * 60)
    print(f"generated {len(saved)}/{len(PROMPTS)} images")
    for p in saved:
        print(f"  {p}")
    if saved:
        print("\nOpen these images to judge quality before Stage 2 (wiring into Ava).")
    else:
        print("\nNo images generated — check the network / error lines above.")


if __name__ == "__main__":
    main()
