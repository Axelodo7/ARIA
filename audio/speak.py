"""Text-to-speech (ElevenLabs streaming) + local WAV playback via sounddevice.

speak(text) streams eleven_turbo_v2 PCM and plays it as it arrives.
play_wav(path) plays a pre-generated WAV (used for the cached "Yes?" ack, which
never hits the API). Both return True/False and never raise for handled
failures — the caller's per-turn guard stays simple. Extracted from
scripts/test_tts.py.
"""
import time
import wave

import numpy as np
import sounddevice as sd

import config

SAMPLE_RATE = 24000
OUTPUT_FORMAT = "pcm_24000"

_client = None


def _client_or_none():
    global _client
    if not config.ELEVENLABS_API_KEY:
        return None
    if _client is None:
        from elevenlabs.client import ElevenLabs
        _client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)
    return _client


def speak(text):
    """Stream `text` via ElevenLabs and play it. Returns True on success."""
    if not text:
        return False
    client = _client_or_none()
    if client is None:
        print("[speak] ELEVENLABS_API_KEY missing — cannot speak.")
        return False

    from elevenlabs.core import ApiError
    import httpx

    play_start, bytes_played = None, 0
    try:
        audio_iter = client.text_to_speech.stream(
            config.ELEVENLABS_VOICE_ID, text=text, model_id=config.TTS_MODEL,
            output_format=OUTPUT_FORMAT)
        with sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as out:
            for chunk in audio_iter:
                if not chunk:
                    continue
                if play_start is None:
                    play_start = time.time()
                out.write(chunk)
                bytes_played += len(chunk)
            if play_start is not None:  # let the tail finish
                remaining = (play_start + bytes_played / 2 / SAMPLE_RATE) - time.time()
                if remaining > 0:
                    time.sleep(remaining)
        return play_start is not None
    except ApiError as e:
        print(f"[speak] ElevenLabs API error ({getattr(e, 'status_code', None)}).")
        return False
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.RemoteProtocolError, ConnectionError):
        print("[speak] Network unavailable — couldn't reach ElevenLabs.")
        return False
    except Exception as e:
        print(f"[speak] TTS failed: {type(e).__name__}.")
        return False


def play_wav(path):
    """Play a WAV file through the default output device."""
    try:
        with wave.open(path, "rb") as wf:
            sr = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        sd.play(np.frombuffer(frames, dtype=np.int16), sr)
        sd.wait()
        return True
    except Exception as e:
        print(f"[play_wav] failed for {path}: {type(e).__name__}.")
        return False
