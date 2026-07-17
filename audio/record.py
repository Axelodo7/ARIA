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


def record_followup(window_seconds, max_utterance_seconds=30):
    """Post-reply follow-up capture. Wait up to `window_seconds` for speech to START
    (VAD), then record the whole utterance until silence — so a late-starting
    follow-up is not truncated at the window edge. Returns (audio, got_speech):
    (float32 array, True) if speech began within the window, else (empty, False).

    This is what makes the window VAD-gated rather than 'record N seconds and hope':
    background silence returns got_speech=False and the caller drops to idle, and
    only a real speech onset opens a turn."""
    vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
    silence_limit_frames = max(1, int(config.VAD_SILENCE_LIMIT_SEC * 1000 / FRAME_MS))
    onset_deadline_frames = max(1, int(window_seconds * 1000 / FRAME_MS))
    max_utterance_frames = int(max_utterance_seconds * 1000 / FRAME_MS)

    frames = []
    silence_run = 0
    started = False
    waited = 0

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=FRAME_SAMPLES) as stream:
        while True:
            data, _overflow = stream.read(FRAME_SAMPLES)
            frame = bytes(data)
            if len(frame) < FRAME_SAMPLES * 2:
                continue
            speech = vad.is_speech(frame, SAMPLE_RATE)
            if not started:
                if speech:
                    started = True
                    frames.append(frame)          # keep the onset frame onward
                else:
                    waited += 1
                    if waited >= onset_deadline_frames:
                        return np.zeros(0, dtype=np.float32), False   # window elapsed
                continue
            frames.append(frame)
            silence_run = 0 if speech else silence_run + 1
            if silence_run >= silence_limit_frames or len(frames) >= max_utterance_frames:
                break

    audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    return audio, True
