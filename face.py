"""Jugnu's face: a see-through window showing face/face.html, so it floats over
the desktop. Waking, it rises out of a soft light at the window's bottom edge;
asleep, it sinks back in and the window hides.

The first time it opens big on your last screen (your second monitor, if you
have one), bottom-centred. Over the face: drag to move it, Ctrl + scroll to
resize, scroll alone to set its brightness. Empty space around it lets clicks
through, and other windows can cover it. Where you leave it, and how bright,
is remembered in logs/face.json. "Hide face" in the tray menu tucks it away;
closing the window does the same. Quit stops it.

About 30 times a second it tells the page its state, how loud it is, the shape
of its voice in 8 bands, and how loud you are.
"""
import json
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "face" / "face.html"
SAVED = ROOT / "logs" / "face.json"
EDGES = np.geomspace(90, 7000, 9)  # 8 bands, low to high, across the voice's range
FADE = 1.6                          # seconds it takes to sink away before the window hides

WINDOW = None   # the open face, for the tray
wanted = True   # False after "Hide face" in the tray: then it stays hidden even when awake
spot = {}       # where it is, how big and how bright: kept here, as the window can't always say


class Loudness:
    """Its loudness 0..1, relative to how loud it's been lately (so a quiet voice
    still moves the orb fully), plus the shape of its voice in 8 bands."""

    def __init__(self):
        self.peak = 0.02

    def __call__(self, chunk, rate):
        if chunk is None or not rate:
            return 0.0, [0.0] * 8
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        self.peak = max(rms, self.peak * 0.997, 0.02)  # slow to forget a loud moment
        loud = 0.0 if rms < 0.004 else min(1.0, rms / (self.peak * 0.75)) ** 0.8
        spec = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk))))
        freq = np.fft.rfftfreq(len(chunk), 1 / rate)
        bands = np.array([spec[(freq >= a) & (freq < b)].mean() if ((freq >= a) & (freq < b)).any() else 0.0
                          for a, b in zip(EDGES, EDGES[1:])])
        bands = bands / (bands.max() + 1e-9) * loud  # the shape, scaled by how loud it is
        return round(loud, 3), [round(float(b), 3) for b in bands]


def mode(s):
    if not s or s.get("loading", True):
        return "asleep"
    if s["talking"]:
        return "talking"
    if s.get("thinking"):
        return "thinking"
    return "listening" if s["awake"] else "asleep"


def first_place(screens):
    """Big on the last screen, bottom-centred, so it rises from its bottom edge."""
    s = screens[-1]
    sx, sy = getattr(s, "x", 0) or 0, getattr(s, "y", 0) or 0
    h = int(s.height * 0.8)
    w = int(h * 0.8)
    return {"x": sx + (s.width - w) // 2, "y": sy + s.height - h, "width": w, "height": h}


def place(screens):
    """Where you left it last time, or the first place; and its brightness."""
    at = {**first_place(screens), "gain": 1.2}
    try:
        at.update({k: v for k, v in json.loads(SAVED.read_text()).items() if k in at})
    except (OSError, ValueError):
        pass
    return at


def remember():
    try:
        SAVED.parent.mkdir(exist_ok=True)
        SAVED.write_text(json.dumps(spot))
    except Exception:  # never worth stopping it over
        pass


class Api:
    """What the page can ask of the window (it asks when you scroll over the face)."""

    def grow(self, factor):  # Ctrl + scroll: resize about the bottom-centre, where it rises from
        w, h = spot["width"], spot["height"]
        nw = int(min(4000, max(240, w * float(factor))))
        nh = int(nw * h / w)
        spot.update(x=spot["x"] + (w - nw) // 2, y=spot["y"] + h - nh, width=nw, height=nh)
        print(f"  (face: {w}x{h} -> {nw}x{nh})", flush=True)
        WINDOW.move(spot["x"], spot["y"])
        WINDOW.resize(nw, nh)
        remember()

    def brighten(self, factor):  # scroll: brighter or dimmer, for whichever screen it's on
        spot["gain"] = round(min(4.0, max(0.25, spot["gain"] * float(factor))), 3)
        print(f"  (face: brightness {spot['gain']})", flush=True)
        remember()

    def log(self, message):  # the page reports its own errors here
        print(f"  (face page: {str(message)[:200]})", flush=True)


def adjust(what):
    """The tray's Face menu: bigger, smaller, brighter, dimmer."""
    if WINDOW:
        {"bigger": lambda: Api().grow(1.15), "smaller": lambda: Api().grow(1 / 1.15),
         "brighter": lambda: Api().brighten(1.2), "dimmer": lambda: Api().brighten(1 / 1.2)}[what]()


COLORS = ROOT / "colors.txt"


def colors():
    """colors.txt: "name = colour" lines (a model or council member, a CSS colour name or #hex)."""
    out = {}
    try:
        for line in COLORS.read_text(encoding="utf-8").splitlines():
            name, eq, colour = line.partition("=")
            if eq and name.strip() and colour.strip() and not line.lstrip().startswith("#"):
                out[name.strip().lower()] = colour.strip()
    except OSError:
        pass
    try:  # and the colours Jugnu gave models you asked for by name ("switch to Hydrogen"): colors.txt wins
        for name, colour in json.loads((ROOT / "logs" / "model_colours.json").read_text(encoding="utf-8")).items():
            out.setdefault(name, colour)
    except (OSError, ValueError):
        pass
    return out


def feed(window, jugnu, alive, floating):
    send = getattr(window, "run_js", None) or window.evaluate_js  # run_js doesn't wait for a reply
    hearing, shown, asleep_since = Loudness(), True, time.monotonic()
    lesson = None  # learning mode, as last sent to the page
    tick = 0
    while alive.is_set():
        tick += 1
        if tick % 60 == 1:  # every couple of seconds: its colours, in case it changed them
            try:
                send(f"window.jugnu && jugnu.colors && jugnu.colors({json.dumps(colors())})")
            except Exception:
                pass
        try:
            s = jugnu.state()
        except Exception:  # caught mid-change: the next look will do, and the face must not stop for it
            time.sleep(1 / 12)
            continue
        m = mode(s)
        loud, bands = hearing(s.get("playing"), s.get("rate"))
        heard = min(1.0, s.get("heard", 0.0) * 8)
        if shown:  # hidden, the page needs nothing
            try:
                send(f"window.jugnu && jugnu.update({json.dumps(m)}, {loud}, {heard:.3f}, {bands}, {spot['gain']}, {json.dumps(s.get('model', ''))}, {json.dumps(s.get('advisers', []))});"
                     f"document.body.classList.toggle('float', {json.dumps(floating)});"
                     f"document.body.classList.toggle('seeing', {json.dumps(bool(s.get('vision')))})")
                if s.get("learning") != lesson:  # the ring, and the line to read inside it
                    send(f"window.jugnu && jugnu.learn && jugnu.learn({json.dumps(s.get('learning'))})")
                    lesson = s.get("learning")
            except Exception:  # page still loading, or the window is going away
                pass
        # asleep: let it sink away, then hide the window; awake: show it and it rises
        now = time.monotonic()
        if m != "asleep":
            asleep_since = None
        elif asleep_since is None:
            asleep_since = now
        visible = wanted and (asleep_since is None or now - asleep_since < FADE)
        if visible != shown:
            (window.show if visible else window.hide)()
            shown = visible
        time.sleep(1 / 30 if m == "talking" else 1 / 12)  # the voice's ridges need 30 a second; the rest far less


def toggle():
    """The tray's Hide/Show face."""
    global wanted
    wanted = not wanted


def engine():
    """Qt (PySide6) if installed: it makes truly see-through windows on Windows. Without it,
    Windows' own WebView2, which can't (it draws a dark box), so the face gets a dark backdrop."""
    import importlib.util
    return "qt" if importlib.util.find_spec("PySide6") and importlib.util.find_spec("qtpy") else None


def show(jugnu, quitting):
    """Opens the face and blocks until Quit (webview needs the main thread on Windows)."""
    import webview

    global WINDOW
    gui = engine()
    spot.update(place(webview.screens))
    window = WINDOW = webview.create_window(
        "Jugnu", str(PAGE), js_api=Api(), x=spot["x"], y=spot["y"], width=spot["width"],
        height=spot["height"], frameless=True, easy_drag=True, on_top=False,
        transparent=gui == "qt", background_color="#03060a")
    print(f"face: {'see-through (Qt)' if gui else 'on a dark backdrop (pip install pywebview[pyside6] to float)'}")

    def closing():  # Alt+F4 hides it, as "Hide face" does; only Quit closes
        global wanted
        if not quitting.is_set():
            wanted = False
            return False

    def moved(x, y):
        spot.update(x=x, y=y)
        remember()

    def resized(width, height):
        spot.update(width=width, height=height)
        remember()

    window.events.closing += closing
    window.events.moved += moved
    window.events.resized += resized
    alive = threading.Event(); alive.set()
    webview.start(feed, (window, jugnu, alive, gui == "qt"), gui=gui, http_server=True)  # serves face/ to the page
    alive.clear()


if __name__ == "__main__":
    hear = Loudness()
    assert hear(None, 0) == (0.0, [0.0] * 8)
    assert hear(np.zeros(480, np.float32), 24000)[0] == 0.0
    t = np.arange(480) / 24000
    tone = lambda f, a: (a * np.sin(2 * np.pi * f * t)).astype(np.float32)
    loud, bands = hear(tone(3000, 0.2), 24000)
    assert loud > 0.9 and int(np.argmax(bands)) == 6, bands  # 3 kHz lands in the 7th band
    assert int(np.argmax(hear(tone(200, 0.2), 24000)[1])) in (1, 2)
    quiet = Loudness()  # a soft voice still moves the orb fully once it's the loudest it's been
    assert quiet(tone(200, 0.03), 24000)[0] > 0.9
    s = {"loading": False, "awake": True, "talking": False, "thinking": True}
    assert mode(s) == "thinking" and mode({**s, "talking": True}) == "talking"
    assert mode({**s, "thinking": False, "awake": False}) == "asleep" and mode({"loading": True}) == "asleep"
    Screen = type("Screen", (), {})
    a, b = Screen(), Screen()
    a.x, a.y, a.width, a.height = 0, 0, 1920, 1080
    b.x, b.y, b.width, b.height = 1920, 0, 2560, 1440
    p = first_place([a, b])
    assert p["x"] > 1920 and p["y"] + p["height"] == 1440 and p["height"] == 1152, p
    WINDOW = type("W", (), {"move": lambda *a: None, "resize": lambda *a: None})()
    spot.update(x=100, y=100, width=400, height=500, gain=1.2)
    import tempfile
    SAVED = Path(tempfile.mkdtemp()) / "face.json"  # these checks mustn't move its real face (logs/face.json)
    Api().grow(1.5)
    assert spot["width"] == 600 and spot["height"] == 750 and spot["y"] + 750 == 600 and spot["x"] + 300 == 300, spot  # same bottom, same centre
    for _ in range(50):
        Api().brighten(1.08)
    assert spot["gain"] == 4.0
    print("ok")
