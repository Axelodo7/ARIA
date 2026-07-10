"""Speech-to-text via faster-whisper.

The model is loaded ONCE at import and held resident for the whole process, so
per-utterance latency is just inference (~1-2s), not a reload. Extracted from
scripts/test_stt.py.
"""
import os
import sys

from faster_whisper import WhisperModel

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config

# Model size lives in config (default medium.en; override via WHISPER_MODEL_SIZE).
MODEL_SIZE = config.WHISPER_MODEL_SIZE

# Resident model — loaded on first import of this module.
_model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")


def transcribe(audio):
    """Transcribe a float32 numpy array (16kHz mono) to text. Empty in -> ''."""
    if audio is None or len(audio) == 0:
        return ""
    segments, _info = _model.transcribe(audio, language="en", beam_size=1)
    return "".join(seg.text for seg in segments).strip()
