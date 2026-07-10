"""Wake-word detection via openWakeWord (stock hey_jarvis model).

The model is loaded ONCE at import. listen_for_wake_word() blocks until the wake
word is detected (confidence >= WAKE_WORD_THRESHOLD) and returns the peak
confidence. Extracted from scripts/test_wake_word.py.
"""
import numpy as np
import sounddevice as sd
import openwakeword
from openwakeword.model import Model

import config

SAMPLE_RATE = 16000
FRAME = 1280  # 80ms at 16kHz
WAKEWORD = "hey_jarvis"

# Ensure model files are present (idempotent) and load the model once.
openwakeword.utils.download_models([WAKEWORD])
_model = Model(wakeword_models=[WAKEWORD], inference_framework="onnx")


def listen_for_wake_word():
    """Block until 'hey Jarvis' is detected. Returns the peak confidence."""
    threshold = config.WAKE_WORD_THRESHOLD
    try:
        _model.reset()  # clear any residual state from a previous detection
    except Exception:
        pass

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=FRAME) as stream:
        while True:
            data, _overflow = stream.read(FRAME)
            frame = np.frombuffer(bytes(data), dtype=np.int16)
            scores = _model.predict(frame)
            conf = scores.get(WAKEWORD)
            if conf is None:
                conf = max(scores.values()) if scores else 0.0
            if conf >= threshold:
                return conf
