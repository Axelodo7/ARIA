"""Step 6 VAD test — silence-gated recording -> faster-whisper.

Records from the default mic and stops automatically once you go quiet:
webrtcvad classifies each 30ms frame as speech/silence, and after
VAD_SILENCE_LIMIT_SEC of continuous silence (following some speech) the
recording ends. Then it transcribes with faster-whisper (small.en, CPU int8).

Config (config.py / .env): VAD_AGGRESSIVENESS (0-3, default 2),
VAD_SILENCE_LIMIT_SEC (default 1.2).

Prints: exact recording length, transcription time, transcribed text, with a
live "Listening..." status that flips to "Silence detected, wrapping up...".

Three failure modes -> clear one-line message (no traceback):
  (a) no microphone
  (b) unexpected VAD/recording failure
  (c) empty audio (you didn't speak, or spoke too briefly)

Run from the project root and SPEAK, then pause:
    python scripts/test_vad.py
    python scripts/test_vad.py --max-recording-sec 20

--max-recording-sec (default 15) is a hard safety cap so VAD misbehaving can't
record forever.
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
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000     # 480 samples
MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small.en")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-recording-sec", type=float, default=15,
                        help="Hard cap so VAD can't record forever (default 15).")
    args = parser.parse_args()

    import numpy as np
    import sounddevice as sd

    aggressiveness = config.VAD_AGGRESSIVENESS
    silence_limit = config.VAD_SILENCE_LIMIT_SEC
    silence_limit_frames = max(1, int(silence_limit * 1000 / FRAME_MS))
    max_frames = int(args.max_recording_sec * 1000 / FRAME_MS)

    # Failure (a) — no microphone.
    try:
        sd.check_input_settings(samplerate=SAMPLE_RATE, channels=1)
    except Exception:
        print("[x] No microphone available (default input device can't record "
              "16kHz mono). Check your mic.")
        return

    try:
        import webrtcvad
        vad = webrtcvad.Vad(aggressiveness)
    except Exception as e:
        print(f"[x] Could not initialize webrtcvad: {type(e).__name__}: {str(e)[:120]}")
        return

    print(f"VAD test — aggressiveness={aggressiveness}, silence_limit={silence_limit}s, "
          f"max={args.max_recording_sec}s")
    print("Get ready to speak.")
    for n in (3, 2, 1):
        print(f"  Recording in {n}...", flush=True)
        time.sleep(1)
    print("🎙  Listening — speak now, then pause when done.", flush=True)

    frames = []
    silence_run = 0
    started = False
    triggered = False
    t_rec = time.time()

    # Failure (b) — anything unexpected in the capture/VAD loop.
    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                               blocksize=FRAME_SAMPLES) as stream:
            for i in range(max_frames):
                data, _overflow = stream.read(FRAME_SAMPLES)
                frame = bytes(data)
                if len(frame) < FRAME_SAMPLES * 2:
                    continue
                frames.append(frame)
                is_speech = vad.is_speech(frame, SAMPLE_RATE)
                if is_speech:
                    started = True
                    silence_run = 0
                elif started:
                    silence_run += 1

                if i % 5 == 0:
                    elapsed = (i + 1) * FRAME_MS / 1000
                    state = "speech " if is_speech else "silence"
                    tail = silence_run * FRAME_MS / 1000
                    sys.stdout.write(f"\r   listening {elapsed:4.1f}s  [{state}]  "
                                     f"silence-tail {tail:3.1f}s   ")
                    sys.stdout.flush()

                if started and silence_run >= silence_limit_frames:
                    triggered = True
                    break
    except Exception as e:
        print(f"\n[x] VAD/recording failure: {type(e).__name__}: {str(e)[:120]}")
        return

    rec_s = time.time() - t_rec
    sys.stdout.write("\n")
    if triggered:
        print("   Silence detected, wrapping up...")
    else:
        print(f"   Hit the {args.max_recording_sec:.0f}s safety cap (VAD never saw a clear pause).")
    print(f"  recording length: {rec_s:.2f}s  ({len(frames)} frames)")

    # Failure (c) — empty audio / spoke too briefly.
    if not started:
        print("[x] No speech detected — you didn't speak, spoke too briefly, or "
              "the mic was too quiet.")
        return

    audio = (np.frombuffer(b"".join(frames), dtype=np.int16)
             .astype(np.float32) / 32768.0)

    from faster_whisper import WhisperModel
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    t_tr = time.time()
    segments, _info = model.transcribe(audio, language="en", beam_size=1)
    text = "".join(seg.text for seg in segments).strip()
    tr_s = time.time() - t_tr
    print(f"  transcription: {tr_s:.2f}s")

    if not text:
        print("[x] Transcription returned empty — spoke too briefly or unclear. Retry.")
        return

    print(f"\nTranscribed text: {text!r}")


if __name__ == "__main__":
    main()
