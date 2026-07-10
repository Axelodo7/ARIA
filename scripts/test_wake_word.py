"""Step 6 wake-word test — openWakeWord 'hey_jarvis' on the default mic.

Loads the stock hey_jarvis model and listens continuously, printing a live
confidence score once a second. When confidence crosses WAKE_WORD_THRESHOLD it
prints a detection line with the peak score and exits. (Custom "hey Aria"
training is a later polish task; this uses the pretrained hey_jarvis model.)

Config (config.py / .env): WAKE_WORD_THRESHOLD (default 0.5).

Three failure modes -> clear one-line message (no traceback):
  (a) microphone unavailable
  (b) openWakeWord model download fails on first run
  (c) model file not found on a subsequent run

Run from the project root and say "hey Jarvis":
    python scripts/test_wake_word.py
    python scripts/test_wake_word.py --timeout-sec 30

--timeout-sec (default 60) stops the script if the wake word never fires.

First run downloads the ONNX model files (~30MB) — a "Downloading..." message
is printed so you know it isn't stuck.
"""
import argparse
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

SAMPLE_RATE = 16000
FRAME = 1280  # openWakeWord expects 80ms frames at 16kHz
WAKEWORD = "hey_jarvis"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-sec", type=float, default=60,
                        help="Give up after this many seconds (default 60).")
    args = parser.parse_args()

    import numpy as np
    import sounddevice as sd

    threshold = config.WAKE_WORD_THRESHOLD

    # Failure (a) — no microphone.
    try:
        sd.check_input_settings(samplerate=SAMPLE_RATE, channels=1)
    except Exception:
        print("[x] No microphone available (default input device can't record "
              "16kHz mono). Check your mic.")
        return

    import openwakeword
    from openwakeword.model import Model

    # Failure (b) — model download on first run.
    try:
        print("Downloading model files (first run ~30MB; skipped if already present)...",
              flush=True)
        openwakeword.utils.download_models([WAKEWORD])
    except Exception as e:
        print(f"[x] openWakeWord model download failed: {type(e).__name__}: "
              f"{str(e)[:120]}  (check your connection and retry)")
        return

    # Failure (c) — model file missing on a later run.
    try:
        oww = Model(wakeword_models=[WAKEWORD], inference_framework="onnx")
    except FileNotFoundError as e:
        print(f"[x] openWakeWord model file not found: {str(e)[:120]}  "
              "(delete the openwakeword model cache and rerun to re-download).")
        return
    except Exception as e:
        print(f"[x] Failed to load openWakeWord model: {type(e).__name__}: {str(e)[:120]}")
        return

    print(f"\n🎧 Listening for 'hey Jarvis' — threshold {threshold}, timeout "
          f"{args.timeout_sec:.0f}s. Say it clearly.\n")

    peak = 0.0
    last_print = 0.0
    start = time.time()
    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                               blocksize=FRAME) as stream:
            while time.time() - start < args.timeout_sec:
                data, _overflow = stream.read(FRAME)
                frame = np.frombuffer(bytes(data), dtype=np.int16)
                scores = oww.predict(frame)
                conf = scores.get(WAKEWORD)
                if conf is None:  # fall back to whatever the model reported
                    conf = max(scores.values()) if scores else 0.0
                peak = max(peak, conf)

                now = time.time()
                if now - last_print >= 1.0:
                    print(f"  confidence: {conf:.3f}  (peak {peak:.3f})", flush=True)
                    last_print = now

                if conf >= threshold:
                    print(f"\n🚨 Wake word detected! peak confidence {conf:.3f}")
                    return
    except Exception as e:
        print(f"\n[x] Recording/detection failure: {type(e).__name__}: {str(e)[:120]}")
        return

    print(f"\n[timeout] No wake word within {args.timeout_sec:.0f}s "
          f"(peak confidence {peak:.3f}).")


if __name__ == "__main__":
    main()
