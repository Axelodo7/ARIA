"""Regenerate assets/aria_ack.wav — ARIA's spoken 'Yes?' acknowledgment.

Run once at build time (or any time you want to re-record it). The WAV is played
back locally on every wake, so this ElevenLabs call happens only here — zero API
cost per wake.

    python scripts/regenerate_ack.py
    python scripts/regenerate_ack.py "At your service?"   # custom phrase
"""
import os
import sys
import wave

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config

SAMPLE_RATE = 24000
OUT_PATH = os.path.join(PROJECT_ROOT, "assets", "aria_ack.wav")


def main():
    text = " ".join(sys.argv[1:]).strip() or "Yes?"
    if not config.ELEVENLABS_API_KEY:
        print("[x] ELEVENLABS_API_KEY missing in .env — cannot generate the ack.")
        return

    from elevenlabs.client import ElevenLabs
    client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)

    print(f"Generating ack {text!r} via ElevenLabs ({config.TTS_MODEL})...")
    pcm = b"".join(
        chunk for chunk in client.text_to_speech.stream(
            config.ELEVENLABS_VOICE_ID, text=text, model_id=config.TTS_MODEL,
            output_format="pcm_24000") if chunk)

    if not pcm:
        print("[x] No audio returned from ElevenLabs.")
        return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with wave.open(OUT_PATH, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)

    print(f"Wrote {OUT_PATH}  ({len(pcm)} bytes, {len(pcm) / 2 / SAMPLE_RATE:.2f}s)")


if __name__ == "__main__":
    main()
