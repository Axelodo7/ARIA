"""Step 6 STT smoke test — sounddevice mic capture -> faster-whisper.

Records exactly 5 seconds from the default microphone (sounddevice, 16kHz mono),
then transcribes with faster-whisper (small.en, CPU, int8) and prints the text.

Timings printed: recording, model load (first run downloads the model — slow),
transcription, and total wall-clock.

Three failure modes handled with a clear one-line message (no traceback):
  (a) no microphone available
  (b) model download/load fails
  (c) transcription returns empty (no speech detected)

Run from the project root, and SPEAK during the 5-second window:
    python scripts/test_stt.py

Model size is small.en; override with WHISPER_MODEL_SIZE=base.en (faster) or
medium.en (more accurate). Throwaway test — audio/transcribe.py comes after the
pipeline is proven.
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SAMPLE_RATE = 16000      # what Whisper expects
DURATION = 5             # seconds
MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small.en")


def main():
    import numpy as np  # noqa: F401 (sounddevice returns numpy arrays)
    import sounddevice as sd

    t_total = time.time()

    # Failure (a) — no microphone / can't record the format we need.
    try:
        sd.check_input_settings(samplerate=SAMPLE_RATE, channels=1)
    except Exception:
        print("[x] No microphone available (default input device can't record "
              "16kHz mono). Check your mic and OS input settings.")
        return

    print(f"STT test — model={MODEL_SIZE}, {DURATION}s of audio at {SAMPLE_RATE} Hz.")
    print("Get ready to speak.")
    for n in (3, 2, 1):
        print(f"  Recording in {n}...", flush=True)
        time.sleep(1)
    print("  GO — speak now!", flush=True)

    t_rec = time.time()
    try:
        audio = sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                       channels=1, dtype="float32")
        sd.wait()
    except Exception as e:
        print(f"[x] Recording failed (microphone/device error): {type(e).__name__}.")
        return
    rec_s = time.time() - t_rec
    audio = audio.flatten()
    print(f"  recording: {rec_s:.2f}s")

    # Failure (b) — model download/load.
    t_load = time.time()
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    except Exception as e:
        print(f"[x] Whisper model '{MODEL_SIZE}' download/load failed: "
              f"{type(e).__name__}: {str(e)[:140]}")
        return
    load_s = time.time() - t_load
    print(f"  model load: {load_s:.2f}s")

    # Transcription.
    t_tr = time.time()
    try:
        segments, _info = model.transcribe(audio, language="en", beam_size=1)
        text = "".join(seg.text for seg in segments).strip()
    except Exception as e:
        print(f"[x] Transcription failed: {type(e).__name__}: {str(e)[:140]}")
        return
    tr_s = time.time() - t_tr
    print(f"  transcription: {tr_s:.2f}s")

    # Failure (c) — empty transcription.
    if not text:
        print("[x] Transcription returned empty — no speech detected. Speak "
              "closer/louder to the mic and retry.")
        return

    print(f"  total wall-clock: {time.time() - t_total:.2f}s")
    print(f"\nTranscribed text: {text!r}")


if __name__ == "__main__":
    main()
