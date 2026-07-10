"""main.py — ARIA's voice loop (wake word -> record -> transcribe -> brain ->
speak), with the HUD reflecting each state.

Threading: pywebview requires its GUI loop on the MAIN thread, so the HUD runs on
main and the voice loop runs in a pywebview worker thread (started by hud.run).
Startup warms the fast model and loads Whisper (resident) before the GUI starts.

Fire-and-forget dispatch — ARIA doesn't wait for the teammate (that's Step 7).

Run it (needs mic, speakers, and .env with ElevenLabs + Slack creds):
    python main.py
Stop it with Ctrl+C.
"""
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config

ACK_WAV = os.path.join(PROJECT_ROOT, "assets", "aria_ack.wav")

# --- Logging (per-turn errors go here, not to the user's face) --------------
_LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
logger = logging.getLogger("aria.main")
logger.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(_LOG_DIR, "aria.log"),
                         maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_h)


def main():
    print("Starting ARIA...")
    logger.info("ARIA starting up.")
    if config.DRY_RUN:
        print("  [!] DRY_RUN is True — dispatches are simulated (no real Slack "
              "posts / agent cycles). Set DRY_RUN=False in .env for live use.")

    # Heavy imports here (not at module top) so the progress messages are
    # accurate — each loads its resident model on the main thread before the GUI.
    from brain.aria_agent import Aria, warmup
    from brain import conversation
    print("  warming up the fast model...")
    warmup()
    print("  loading Whisper model...")
    from audio import transcribe
    print("  loading wake-word model (first run downloads ~8MB)...")
    from audio import wake_word
    from audio import record, speak
    from actions import hud

    hud.create_window()
    aria = Aria(hud_hook=hud.set_state)
    stop = threading.Event()

    def run_turn():
        """One full exchange. Any exception is caught by the loop guard."""
        hud.set_state("listening")
        speak.play_wav(ACK_WAV)                 # instant cached "Yes?"
        audio = record.record_until_silence()   # stops when you go quiet

        hud.set_state("thinking")
        text = transcribe.transcribe(audio)
        if not text.strip():
            hud.set_state("speaking")
            speak.speak("I didn't catch that, sir.")
            return
        print(f"  you : {text}")
        logger.info("user: %s", text)

        # Conversation mode? (deterministic; dispatch phrases don't match these,
        # so "ask Bobby about X" still dispatches normally via think().)
        opened = conversation.detect_open(text)
        if opened:
            conversation_mode(*opened)
            return

        result = aria.think(text)               # deterministic routing or model
        reply = result.get("reply", "")
        print(f"  aria: {reply}")
        logger.info("aria: %s (dispatched=%s)", reply, result.get("dispatched"))

        # Fire-and-forget: any dispatch already happened inside think(); here we
        # just speak the confirmation. Direct answers speak the reply. Same path.
        hud.set_state("speaking")
        speak.speak(reply)

    def conversation_mode(agent, topic):
        """Live back-and-forth with one agent until the user exits (or 30s of
        silence). Fire-and-relay: agent replies, ARIA's voice reads it."""
        hud.set_state("speaking")
        speak.speak(f"Connecting you to {agent.capitalize()}.")
        session = conversation.ConversationSession(agent, topic)
        session.open()
        logger.info("conversation opened with %s (topic=%s)", agent, topic)
        try:
            while not stop.is_set():
                hud.set_state("listening")
                audio = record.record_until_silence(max_seconds=30)  # 30s silence -> exit
                hud.set_state("thinking")
                text = transcribe.transcribe(audio)
                if not text.strip():
                    break  # timed out (no speech in 30s)
                print(f"  you->{agent}: {text}")
                if conversation.is_exit(text):
                    break
                reply = session.agent_reply(text)
                session.mirror(text, reply)
                print(f"  {agent}: {reply}")
                logger.info("%s: %s", agent, reply)
                hud.set_state("speaking")
                speak.speak(reply)
        finally:
            session.close()
            hud.set_state("speaking")
            speak.speak("Back to standby.")

    def voice_loop():
        """Runs in pywebview's worker thread once the HUD bridge is ready."""
        hud.set_state("idle")
        print("\n" + "=" * 56)
        print("  ARIA ready — say 'hey Jarvis' to begin.")
        print("=" * 56 + "\n", flush=True)
        while not stop.is_set():
            hud.set_state("idle")
            try:
                wake_word.listen_for_wake_word()   # blocks until triggered
                if stop.is_set():
                    break
                run_turn()
            except Exception:
                logger.exception("per-turn error")
                try:
                    hud.set_state("speaking")
                    speak.speak("Sorry sir, something went wrong.")
                except Exception:
                    pass

    # Ctrl+C: stop the loop and close the window (which returns from hud.run).
    def _shutdown(_signum, _frame):
        print("\nShutting down ARIA...")
        logger.info("ARIA shutting down.")
        stop.set()
        hud.close()

    try:
        signal.signal(signal.SIGINT, _shutdown)
    except Exception:
        pass

    try:
        hud.run(voice_loop)   # GUI loop on the main thread; blocks until close
    except KeyboardInterrupt:
        stop.set()
        hud.close()
    finally:
        stop.set()
        print("ARIA stopped.")
        # The voice-loop worker may be blocked in a mic read; force-exit so the
        # process can't hang waiting on it. Log handlers flush on each emit.
        os._exit(0)


if __name__ == "__main__":
    main()
