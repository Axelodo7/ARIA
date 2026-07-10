"""Step 7B check — speak one line in each agent's ElevenLabs voice.

Plays "Sarah here." in Sarah's voice, "Tom here." in Tom's, etc., one after
another, so you can confirm all four agent voices are wired correctly WITHOUT
needing your mic. Any agent whose *_VOICE_ID is unset falls back to ARIA's voice
(with a warning) instead of crashing — so if two lines sound identical, that
agent's voice ID is missing from .env.

Run from the project root (needs ELEVENLABS_API_KEY + the *_VOICE_ID vars in .env):
    python scripts/test_agent_voices.py
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
from audio import speak
from brain.conversation import resolve_agent_voice

AGENTS = ["Sarah", "Tom", "Ava", "Bobby"]


def _mask(voice_id):
    if not voice_id:
        return "(unset -> ARIA fallback)"
    return f"...{voice_id[-4:]}" if len(voice_id) > 4 else voice_id


def main():
    print("=" * 60)
    print("Agent voices — one line per agent (Step 7B)")
    print("=" * 60)

    if not config.ELEVENLABS_API_KEY:
        print("[x] ELEVENLABS_API_KEY missing in .env — cannot speak.")
        return

    print(f"  ARIA voice (default): {_mask(config.ELEVENLABS_VOICE_ID)}\n")

    resolved, played = {}, []
    for name in AGENTS:
        voice = resolve_agent_voice(name)          # None -> ARIA's voice (warned)
        resolved[name] = voice
        line = f"{name} here."
        print(f"  {name:<6} voice {_mask(voice):<28} speaking: {line!r}")
        ok = speak.speak(line, voice_id=voice)
        played.append(ok)
        if not ok:
            print(f"    [!] playback failed for {name}.")
        time.sleep(0.6)                            # a beat between voices

    # --- summary ------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    all_played = all(played)
    all_set = all(resolved[n] for n in AGENTS)
    distinct = len({resolved[n] for n in AGENTS}) == len(AGENTS)
    missing = [n for n in AGENTS if not resolved[n]]

    print(f"  [{'PASS' if all_played else 'FAIL'}] all four lines played without error")
    print(f"  [{'PASS' if all_set else 'WARN'}] all four agent voice IDs configured"
          + (f" (missing: {', '.join(missing)})" if missing else ""))
    print(f"  [{'PASS' if distinct else 'WARN'}] all four voice IDs are distinct")

    if all_played and all_set and distinct:
        print("\nAll four agent voices are wired and distinct. Confirm by ear that "
              "each line sounded like a different voice.")
    elif not all_set:
        print("\nSome *_VOICE_ID vars are unset in .env, so those agents used ARIA's "
              "voice. Add distinct Voice IDs (elevenlabs.io -> Voice Library) and "
              "rerun to hear four distinct voices.")
    elif not distinct:
        print("\nVoice IDs are set but not all distinct — two agents share an ID. "
              "Give each a unique Voice ID in .env.")


if __name__ == "__main__":
    main()
