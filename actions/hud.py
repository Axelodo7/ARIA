"""pywebview HUD driver.

IMPORTANT threading note: pywebview requires its GUI event loop to run on the
MAIN thread (it raises otherwise). So the model is inverted from a naive
"HUD in a background thread": run() runs the GUI loop on the main thread and
executes your voice loop in a pywebview worker thread once the window is ready.
set_state(state) drives the window.setState bridge (verified in test_hud.py).
"""
import os
import threading
import time

import webview

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(_ROOT, "ui", "index.html")
_VALID = ("idle", "listening", "thinking", "speaking")

_window = None
_ready = threading.Event()


def _primary_work_area():
    """(x, y, w, h) of the primary display's work area — the full screen minus
    the taskbar — on Windows. Returns None off Windows or on error."""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        # SPI_GETWORKAREA (0x0030): screen area not covered by the taskbar.
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return None
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        return None


def create_window():
    """Create the HUD window (call on the main thread). Sized to fill the primary
    display's work area (maximized-borderless — taskbar stays visible, Alt+F4 to
    close). Stays frameless, always-on-top, and transparent. Returns the window."""
    global _window
    kwargs = dict(frameless=True, on_top=True, transparent=True)
    area = _primary_work_area()
    if area:
        x, y, w, h = area
        kwargs.update(x=x, y=y, width=w, height=h)
    else:
        kwargs.update(maximized=True)  # fallback (non-Windows / query failed)
    _window = webview.create_window("ARIA", url=INDEX_HTML, **kwargs)
    return _window


def run(entrypoint):
    """Run pywebview's GUI loop on the CURRENT (main) thread and call
    `entrypoint()` in a worker thread once the setState bridge is attached.
    BLOCKS until the window closes."""
    if _window is None:
        create_window()

    def _bootstrap(window):
        for _ in range(50):  # wait up to ~5s for window.setState to attach
            try:
                if window.evaluate_js("typeof window.setState") == "function":
                    break
            except Exception:
                pass
            time.sleep(0.1)
        _ready.set()
        entrypoint()

    webview.start(_bootstrap, _window)


def wait_ready(timeout=20):
    return _ready.wait(timeout)


def set_state(state):
    """Set the HUD state (idle/listening/thinking/speaking). No-op if the HUD
    isn't up, and never raises — a HUD glitch must not break the voice loop."""
    if state not in _VALID or _window is None or not _ready.is_set():
        return
    try:
        _window.evaluate_js(f'window.setState("{state}")')
    except Exception:
        pass


def close():
    """Destroy the HUD window, which lets run()/webview.start return."""
    global _window
    try:
        if _window is not None:
            _window.destroy()
    except Exception:
        pass
