"""VAD-gated recording via sounddevice + webrtcvad.

record_until_silence() captures from the default mic and stops once you go quiet
(VAD_SILENCE_LIMIT_SEC of continuous silence after some speech). Returns a
float32 numpy array (16kHz mono) ready to hand straight to transcribe() — no
temp WAV needed. Extracted from scripts/test_vad.py.
"""
import numpy as np
import sounddevice as sd
import webrtcvad

import config

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480


def record_until_silence(max_seconds=15):
    """Record until VAD detects the end of speech (or the safety cap). Returns a
    float32 numpy array (empty if no speech was detected)."""
    vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
    silence_limit_frames = max(1, int(config.VAD_SILENCE_LIMIT_SEC * 1000 / FRAME_MS))
    max_frames = int(max_seconds * 1000 / FRAME_MS)

    frames = []
    silence_run = 0
    started = False

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=FRAME_SAMPLES) as stream:
        for _ in range(max_frames):
            data, _overflow = stream.read(FRAME_SAMPLES)
            frame = bytes(data)
            if len(frame) < FRAME_SAMPLES * 2:
                continue
            frames.append(frame)
            if vad.is_speech(frame, SAMPLE_RATE):
                started = True
                silence_run = 0
            elif started:
                silence_run += 1
            if started and silence_run >= silence_limit_frames:
                break

    if not started:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
