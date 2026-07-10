"""Step 6 HUD test — launch ui/index.html in pywebview and drive setState.

Opens the HUD in a frameless, always-on-top, transparent pywebview window, then
cycles through all four states (idle -> listening -> thinking -> speaking ->
idle) with 3s pauses, driving each transition via window.setState(...). The
state name is printed to the terminal as it fires so you can watch the terminal
and the window change together. After the cycle it holds on idle for 2s and
closes cleanly.

Three failure modes -> clear one-line message (no traceback):
  (a) ui/index.html missing
  (b) pywebview backend not installed / won't launch
  (c) evaluate_js can't find window.setState (the bridge isn't intact)

Run from the project root:
    python scripts/test_hud.py

Note: on Windows pywebview uses the Edge WebView2 runtime. If launch fails with
a WebView2 error, that runtime needs its (small) installer.
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(PROJECT_ROOT, "ui", "index.html")
STATES = ("idle", "listening", "thinking", "speaking", "idle")


def _cycle(window):
    """Runs in pywebview's worker thread once the GUI loop is up."""
    try:
        time.sleep(1.5)  # let the DC component mount + attach window.setState

        try:
            kind = window.evaluate_js("typeof window.setState")
        except Exception as e:
            print(f"[x] evaluate_js failed during bridge check: "
                  f"{type(e).__name__}: {str(e)[:120]}")
            return
        if kind != "function":
            print(f"[x] setState bridge NOT intact — typeof window.setState = "
                  f"{kind!r}. The HUD's componentDidMount didn't attach it.")
            return
        print("  setState bridge: OK (window.setState is a function)")

        for state in STATES:
            print(f"  state -> {state}", flush=True)
            result = window.evaluate_js(f'window.setState("{state}")')
            if result is False:
                print(f"  [x] HUD rejected setState({state!r}) — unexpected.")
            time.sleep(3)

        print("  holding on idle for 2s, then closing...")
        time.sleep(2)
    finally:
        window.destroy()  # always close so webview.start() returns


def main():
    # Failure (a) — HTML missing.
    if not os.path.exists(INDEX_HTML):
        print(f"[x] ui/index.html not found at {INDEX_HTML}.")
        return

    # Failure (b, part 1) — pywebview not installed.
    try:
        import webview
    except ImportError:
        print("[x] pywebview is not installed. Run: pip install pywebview")
        return

    window = webview.create_window(
        "ARIA", url=INDEX_HTML,
        frameless=True, on_top=True, transparent=True,
        width=720, height=800,
    )

    # Failure (b, part 2) — backend won't launch (e.g. missing WebView2 runtime).
    try:
        webview.start(_cycle, window)
    except Exception as e:
        msg = str(e)
        if "webview2" in msg.lower() or "edge" in msg.lower() or "runtime" in msg.lower():
            print(f"[x] pywebview backend failed — the Edge WebView2 runtime may be "
                  f"missing (small installer needed). Detail: {msg[:140]}")
        else:
            print(f"[x] pywebview failed to launch: {type(e).__name__}: {msg[:140]}")
        return

    print("HUD closed cleanly.")


if __name__ == "__main__":
    main()
