"""Step 6 TTS smoke test — ElevenLabs streaming -> sounddevice playback.

Speaks one sentence via ElevenLabs (eleven_turbo_v2, streaming PCM) and plays it
through the default output device, reporting:
  - time-to-first-audio: API call sent -> first audio byte received (the number
    that governs how snappy the voice feels)
  - total wall-clock: script start -> playback finished

Three failure modes are handled with a clear one-line message (no traceback):
  (a) missing / invalid API key
  (b) missing / invalid voice ID
  (c) network unavailable

Run from the project root:
    python scripts/test_tts.py

Reads ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID / TTS_MODEL from config (i.e. from
your .env). This is a throwaway test — the reusable audio/speak.py comes after
the pipeline is proven.
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

SENTENCE = "Good morning. Systems are online."
SAMPLE_RATE = 24000              # matches pcm_24000
OUTPUT_FORMAT = "pcm_24000"


def main():
    t_script = time.time()
    key = (config.ELEVENLABS_API_KEY or "").strip()
    voice = (config.ELEVENLABS_VOICE_ID or "").strip()
    model = config.TTS_MODEL

    # Failure mode (a) — missing key.
    if not key or key == "sk-your-key-here":
        print("[x] ELEVENLABS_API_KEY is missing. Add it to your .env "
              "(elevenlabs.io -> profile -> API Keys).")
        return
    # Failure mode (b) — missing voice id.
    if not voice:
        print("[x] ELEVENLABS_VOICE_ID is missing. Add it to your .env "
              "(elevenlabs.io -> Voice Library).")
        return

    import sounddevice as sd
    from elevenlabs.client import ElevenLabs
    from elevenlabs.core import ApiError
    import httpx

    print(f"Speaking: {SENTENCE!r}")
    print(f"  model={model}  voice={voice}  format={OUTPUT_FORMAT}")
    client = ElevenLabs(api_key=key)

    print("API call sent")
    t_sent = time.time()
    first_ts = None
    play_start = None
    bytes_played = 0

    try:
        audio_iter = client.text_to_speech.stream(
            voice, text=SENTENCE, model_id=model, output_format=OUTPUT_FORMAT)
        with sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as out:
            for chunk in audio_iter:
                if not chunk:
                    continue
                if first_ts is None:
                    first_ts = time.time()
                    play_start = first_ts
                    print(f"first audio byte received: {(first_ts - t_sent) * 1000:.0f} ms "
                          "(time-to-first-audio)")
                out.write(chunk)
                bytes_played += len(chunk)
            # Let the buffered tail finish playing before we close the stream.
            if play_start is not None:
                audio_secs = bytes_played / 2 / SAMPLE_RATE  # 2 bytes/sample, mono
                remaining = (play_start + audio_secs) - time.time()
                if remaining > 0:
                    time.sleep(remaining)
    except ApiError as e:
        code = getattr(e, "status_code", None)
        detail = str(getattr(e, "body", "") or e)[:160]
        if code == 401:
            print("[x] ElevenLabs rejected the API key (401 unauthorized). "
                  "Check ELEVENLABS_API_KEY.")
        elif code in (400, 404, 422):
            print(f"[x] ElevenLabs rejected the request ({code}) — likely an invalid "
                  f"ELEVENLABS_VOICE_ID. Detail: {detail}")
        else:
            print(f"[x] ElevenLabs API error ({code}). Detail: {detail}")
        return
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.RemoteProtocolError, ConnectionError) as e:
        print("[x] Network unavailable — couldn't reach ElevenLabs. Check your connection.")
        return
    except Exception as e:  # never dump a traceback at the user
        print(f"[x] TTS failed: {type(e).__name__}: {str(e)[:160]}")
        return

    if first_ts is None:
        print("[!] No audio was received from ElevenLabs.")
        return

    print(f"total wall-clock (start -> playback done): {time.time() - t_script:.2f}s")
    print("OK — TTS pipeline works.")


if __name__ == "__main__":
    main()
