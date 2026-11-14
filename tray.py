"""Jugnu in the system tray: an icon by the clock, and its face floating above it.

The icon shows its state: amber loading, grey asleep, green listening, blue
talking, red stopped. Right-click for Wake, Sleep, Allow/Deny (when it asks
permission), Private mode (its voice and ears on your GPU, nothing to Google),
Face (hide, bigger, smaller, brighter, dimmer), Settings (setup again), Show conversation and Quit.
If Google stops answering for half an hour, a pop-up suggests private mode. The face is face.py;
without pywebview installed it runs as just the icon.
What the console version prints goes to logs/console-<date>.txt, and "Show
conversation" opens a live view of it. Only Quit stops it.

start_jugnu.bat runs this. `python jugnu.py` is still the console version.
"""
import asyncio
import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / f"console-{datetime.now():%Y-%m-%d}.txt"
COLORS = {"loading": "#d89614", "asleep": "#7a8394", "awake": "#2f9e5b",
          "talking": "#3b7dd8", "stopped": "#d64545"}
SAYS = {"loading": "loading...", "asleep": "asleep (say Hey Jugnu)", "awake": "listening",
        "talking": "talking", "stopped": "stopped: see Show conversation"}
WINDOWS = sys.platform == "win32"

crashed, finished, quitting = threading.Event(), threading.Event(), threading.Event()
stop_reason = [""]  # why it stopped, for the pop-up


def only_one():
    """Two Jugnus would both answer you, so a second start just exits."""
    if WINDOWS:
        global _mutex  # held for as long as it runs
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JugnuTray")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0, "Jugnu is already running: look for the J by the clock (it may be under the ^).\n\n"
                   "If there's no J, it's stuck: end python.exe in Task Manager, then start it again.",
                "Jugnu", 0x40)
            sys.exit(0)


def hide_console():
    """Started with python.exe, not pythonw, so the Claude and Codex helpers it launches
    share this hidden console instead of each flashing a window of their own."""
    if WINDOWS:
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)


def run_jugnu(jugnu):
    try:
        asyncio.run(jugnu.main(keyboard=False))
    except BaseException as e:  # SystemExit too, e.g. a missing token
        traceback.print_exc()
        stop_reason[0] = str(e) or type(e).__name__
        crashed.set()
    finally:
        finished.set()


def phase(jugnu):
    if crashed.is_set():
        return "stopped"
    s = jugnu.state()
    if s["loading"]:
        return "loading"
    return "talking" if s["talking"] else "awake" if s["awake"] else "asleep"


def picture(color):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    draw.ellipse((2, 2, 62, 62), fill=color)
    try:
        font = ImageFont.load_default(size=40)
    except TypeError:  # older Pillow: small fixed font
        font = ImageFont.load_default()
    draw.text((32, 33), "J", fill="white", font=font, anchor="mm")
    return im


def show_log():
    """A live view of what it prints. Closing it doesn't stop it."""
    if WINDOWS:
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command",
             f"$host.ui.RawUI.WindowTitle = 'Jugnu'; "
             f"Get-Content -Wait -Tail 40 -Encoding UTF8 -LiteralPath '{LOG}'"],
            creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        print(f"(open {LOG})")


def open_setup():
    """The setup window, in a process of its own (pythonw where there is one: no console window)."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(ROOT / "setup_jugnu.py")], cwd=ROOT)


def main():
    import face
    import jugnu
    import pystray

    threading.Thread(target=run_jugnu, args=(jugnu,), daemon=True).start()
    now = {"phase": "loading"}

    def quit_(icon):
        quitting.set()
        jugnu.post("quit", "")
        finished.wait(15)  # let it finish its sentence and close the Claude session
        icon.stop()
        # then end outright: closing the face's Qt window from this thread can hang forever,
        # leaving python.exe behind with no icon. Nothing is left to save by now.
        os._exit(0)


    icon = pystray.Icon("jugnu", picture(COLORS["loading"]), "Jugnu: loading...", pystray.Menu(
        pystray.MenuItem(lambda _: f"Jugnu: {SAYS[now['phase']]}", None, enabled=False),
        pystray.MenuItem("Wake", lambda: jugnu.post("typed", ""), default=True),
        pystray.MenuItem("Sleep", lambda: jugnu.post("sleep", None)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Allow (when it asks)", lambda: jugnu.post("typed", "yes")),
        pystray.MenuItem("Deny", lambda: jugnu.post("typed", "no")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: "Private mode (switching...)" if jugnu.PRIVATE["switching"]
                         else "Private mode (no Google)", lambda: jugnu.toggle_private(),
                         checked=lambda _: jugnu.PRIVATE["on"]),
        pystray.MenuItem("Face", pystray.Menu(
            pystray.MenuItem(lambda _: "Hide" if face.wanted else "Show", face.toggle),
            pystray.MenuItem("Bigger", lambda: face.adjust("bigger")),
            pystray.MenuItem("Smaller", lambda: face.adjust("smaller")),
            pystray.MenuItem("Brighter", lambda: face.adjust("brighter")),
            pystray.MenuItem("Dimmer", lambda: face.adjust("dimmer")))),
        pystray.MenuItem("Only my voice", lambda: jugnu.post("voice", None), checked=lambda _: jugnu.VOICE["only"]),
        pystray.MenuItem("Vision (sees your screen)", lambda: jugnu.post("vision", None), checked=lambda _: jugnu.VISION["on"]),
        pystray.MenuItem("Self-improve mode", lambda: jugnu.post("improve", None), checked=lambda _: jugnu.IMPROVE["on"]),
        pystray.MenuItem("Undo last self-improvement", lambda: jugnu.post("undo", None),
                         visible=lambda _: bool(jugnu.guard.state().get("last"))),
        pystray.MenuItem("Show conversation", show_log),
        # settings are read when it starts, so this stops it; setup's last screen starts it again
        pystray.MenuItem("Settings (restarts Jugnu)", lambda icon: (open_setup(), quit_(icon))),
        pystray.MenuItem("Quit", quit_),
    ))

    def notify(text):
        try:
            icon.notify(text)
        except NotImplementedError:  # no pop-ups on this system
            pass

    def watch():
        told = False  # about Google being down, this time
        shown = None  # private mode as the menu last showed it
        while True:
            private = (jugnu.PRIVATE["on"], jugnu.PRIVATE["switching"])
            if private != shown:  # the menu only redraws its labels when told to
                shown = private
                icon.update_menu()
            try:
                down = jugnu.state().get("google_down", False)
            except Exception:  # caught mid-change: as it was
                down = told
            if down and not told:
                notify("Google isn't answering. Right-click > Private mode to talk with the voice and ears on your laptop.")
            told = down
            p = phase(jugnu)
            if p != now["phase"]:
                now["phase"] = p
                icon.icon, icon.title = picture(COLORS[p]), f"Jugnu: {SAYS[p]}"
                if p == "stopped":
                    notify(f"Jugnu stopped: {stop_reason[0][:150] or 'see Show conversation'}. "
                           "Right-click > Settings to change its setup.")
            time.sleep(0.3)

    threading.Thread(target=watch, daemon=True).start()
    icon.run_detached()
    try:
        face.show(jugnu, quitting)  # blocks until Quit
    except Exception:  # no pywebview, or no WebView2: it still runs, just as the icon
        traceback.print_exc()
        print("(no face window; Jugnu runs from the tray icon)")
    quitting.wait()


if __name__ == "__main__":
    only_one()
    hide_console()
    LOG.parent.mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
    print(f"\n--- started {datetime.now():%H:%M:%S} ---")
    try:
        main()
    except Exception:  # e.g. pystray not installed: say so in a box, there's no console
        traceback.print_exc()
        if WINDOWS:
            ctypes.windll.user32.MessageBoxW(0, traceback.format_exc()[-800:],
                                             "Jugnu couldn't start", 0x10)
        raise
    os._exit(0)  # daemon threads (mic, speakers) hold audio handles; don't wait on them
