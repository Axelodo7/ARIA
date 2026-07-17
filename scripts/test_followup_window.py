"""The post-reply follow-up window: continue talking to ARIA without the wake word.

After ARIA speaks, the mic stays open (VAD-gated) for a configurable window
(FOLLOWUP_WINDOW_SECONDS, default 12). A follow-up spoken in the window is handled
as a continued turn and reopens the window; silence past the window returns to
wake-word/idle; a stray sound (empty / unintelligible transcription) is ignored and
the window stays open — never a garbage turn.

This drives conversation.run_followup_window audio-free: `capture` is scripted
(returns (text, got_speech) — got_speech False == the VAD window elapsed in
silence) and `handle` records calls, so the loop's real classification runs on real
strings without a mic or a model.

Gates:
  * a follow-up in the window -> handled without a wake word, and the window reopens
    (capture is asked again) — a back-and-forth flows,
  * silence (window elapsed) -> return to idle, nothing handled,
  * noise / empty transcription -> ignored, window stays open, NO turn fired,
  * an exit phrase ("that's all" / "thank you") -> ends immediately, returns to idle,
  * ARIA's own voice heard back -> echo, ignored,
  * a persistently noisy room gives up after MAX_FOLLOWUP_NOISE, not forever.

    python scripts/test_followup_window.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ["DRY_RUN"] = "True"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from brain import conversation as C


def chk(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def scripted_capture(script):
    """capture() that yields each (text, got_speech) in turn; ('', False) after."""
    it = iter(script)

    def _cap():
        try:
            return next(it)
        except StopIteration:
            return "", False              # nothing more -> window elapses (idle)
    return _cap


def recording_handle(replies=None):
    """handle(text) that records the texts it was asked to handle and returns a
    canned reply (for echo tracking). Defaults to a fixed reply."""
    handled = []
    rit = iter(replies or [])

    def _handle(text):
        handled.append(text)
        try:
            return next(rit)
        except StopIteration:
            return "Understood, sir."
    return _handle, handled


def main():
    print("=" * 72)
    print("Follow-up window — continue without the wake word; ignore noise; exit")
    print("=" * 72)

    # --- 1) a follow-up is handled and the window REOPENS -------------------
    print("\n--- 1) follow-up spoken in the window ---")
    cap = scripted_capture([("what's the weather", True),
                            ("and the air quality", True),
                            ("", False)])            # then silence -> idle
    handle, handled = recording_handle()
    reason = C.run_followup_window(cap, handle)
    w1 = chk("both follow-ups handled without a wake word",
             handled == ["what's the weather", "and the air quality"])
    w2 = chk("the window reopened after each reply (back-and-forth)", len(handled) == 2)
    w3 = chk("ended by silence -> idle", "silence" in reason)

    # --- 2) silence immediately -> idle, nothing handled -------------------
    print("\n--- 2) silence past the window ---")
    handle, handled = recording_handle()
    reason = C.run_followup_window(scripted_capture([("", False)]), handle)
    s1 = chk("nothing was handled", handled == [])
    s2 = chk("returned to idle (silence)", "silence" in reason)

    # --- 3) noise / empty -> ignored, window stays open -------------------
    print("\n--- 3) noise in the window, then a real follow-up ---")
    cap = scripted_capture([("brr uh", True),        # unintelligible -> noise
                            ("", True),              # empty transcription -> noise
                            ("what's pending", True),  # real -> handled
                            ("", False)])            # silence -> idle
    handle, handled = recording_handle()
    reason = C.run_followup_window(cap, handle)
    n1 = chk("noise did NOT fire a turn (only the real follow-up handled)",
             handled == ["what's pending"])
    n2 = chk("the window stayed open through the noise", len(handled) == 1)

    # --- 4) exit phrase ends immediately ----------------------------------
    print("\n--- 4) exit phrases ---")
    for phrase in ("that's all", "thank you", "goodbye"):
        handle, handled = recording_handle()
        reason = C.run_followup_window(scripted_capture([(phrase, True)]), handle)
        chk(f"{phrase!r} -> exit, nothing handled",
            "exit" in reason and handled == [])
    e1 = True

    # --- 5) echo of ARIA's own reply is ignored ---------------------------
    print("\n--- 5) ARIA's voice heard back ---")
    cap = scripted_capture([("Paris sir", True),     # echo of the seeded reply
                            ("", False)])
    handle, handled = recording_handle()
    reason = C.run_followup_window(cap, handle, initial_recent=["Paris, sir."])
    ec1 = chk("an echo of ARIA's reply is not handled", handled == [])

    # --- 6) a noisy room gives up (not forever) ---------------------------
    print("\n--- 6) persistent noise gives up ---")
    cap = scripted_capture([("brr uh", True)] * 10)  # endless noise
    handle, handled = recording_handle()
    reason = C.run_followup_window(cap, handle)
    r1 = chk(f"gives up after {C.MAX_FOLLOWUP_NOISE} noisy captures",
             "noise" in reason and handled == [])

    # --- 7) shutdown ------------------------------------------------------
    print("\n--- 7) shutdown signal ---")
    handle, handled = recording_handle()
    reason = C.run_followup_window(scripted_capture([("hello", True)]), handle,
                                   should_stop=lambda: True)
    sd1 = chk("a stop signal ends the window immediately",
              "stop" in reason and handled == [])

    all_ok = all([w1, w2, w3, s1, s2, n1, n2, e1, ec1, r1, sd1])
    print("\n" + "=" * 72)
    print("ALL PASS — follow-ups continue without the wake word; noise and silence "
          "are handled; exits work." if all_ok else "SOME CHECKS FAILED.")
    print("=" * 72)
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
