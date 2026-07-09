"""Remotion render wrapper.

Wraps `npx remotion render`, but defaults to a STUB that just writes an empty
file with the right name and prints the args it would have passed. Real
rendering is gated behind config.USE_REAL_REMOTION — right now we're testing
Ava's decision-making, not the render pipeline.
"""
import json
import os
import subprocess

import config


def render(composition, props, dest_path):
    """Render `composition` with `props` to `dest_path`.

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
