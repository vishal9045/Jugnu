"""Jugnu's voice: Google's Chirp 3 HD voice en-IN Algieba (GOOGLE_TTS_API_KEY in .env).
Nothing loads on the GPU, so it's ready in about a second.

If Google can't be reached (no internet, quota, a bad key), it goes quiet and
its replies still show as text; it tries Google again a minute later.
Private mode (the tray) loads Chatterbox Turbo on your GPU instead, so nothing
it says goes to Google; turning it off unloads it and goes back to Google.
Chatterbox copies voices/chatterbox_male.wav, Chatterbox's own male voice (without that file,
its built-in voice).
JUGNU_GOOGLE_VOICE picks another Google voice; JUGNU_VOICE=off keeps it silent.

Speaks sentence by sentence: while one sentence plays, the next is being made.
Everything goes through one Player, which tells the ears exactly what the
speakers are playing so they can cancel it out of the mic.
"""
import base64
import collections
import io
import contextlib
import math
import os
import re
import sys
import threading
import time
import warnings
import wave
from pathlib import Path

import numpy as np

from pronounce import speakable

GOOGLE_VOICE = os.getenv("JUGNU_GOOGLE_VOICE", "en-IN-Chirp3-HD-Algieba")
RATE = 24000      # Google sends 24 kHz audio
OFFLINE = 60      # after Google fails, seconds of silence before trying again
CLONE = Path(__file__).resolve().parent / "voices" / "chatterbox_male.wav"


def plain(text):
    """Written formatting that would otherwise be read out ("asterisk asterisk"): bold,
    bullets, headings, code marks."""
    text = re.sub(r"(?m)^\s*(?:[-*\u2022]|\d+[.)]|#+)\s+", "", text)  # bullets, numbered lists, headings
    text = re.sub(r"\*+|__|`+|~~", "", text)
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # a link: just its words


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


NULL = open(os.devnull, "w")  # never closed: libraries keep a handle to it


_hush = {"depth": 0, "saved": None}
_hush_lock = threading.Lock()


@contextlib.contextmanager
def quiet():
    """Hide the libraries' progress bars and warnings. Errors still show.
    Safe when voice and ears load at the same time: output is muted while any load is
    running and restored when the last one finishes (two plain redirects can restore
    in the wrong order and leave the console muted for good)."""
    with _hush_lock:
        if _hush["depth"] == 0:
            _hush["saved"] = (sys.stdout, sys.stderr)
            sys.stdout = sys.stderr = NULL
        _hush["depth"] += 1
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        with _hush_lock:
            _hush["depth"] -= 1
            if _hush["depth"] == 0:
                sys.stdout, sys.stderr = _hush["saved"]


class Player:
    """One always-open output stream. Each 20 ms it plays also goes to on_play
    (resampled to 16 kHz, silence included), so the ears can cancel the echo."""

    def __init__(self, sd, rate):
        self.sd, self.rate, self.block = sd, rate, rate // 50
        self.chunks = collections.deque()  # (block at `rate`, 320 samples at 16 kHz, its sentence)
        self.on_play = None
        self.silence = (np.zeros(self.block, np.float32), np.zeros(320, np.float32))
        self.last = self.silence[0]  # the 20 ms just played: the face moves to it
        self.now = ""                # the sentence playing right now ("" in silence)
        self.sounded = 0.0           # time.monotonic() it last made a sound: echo checks
        self.open()

    def open(self):
        """On whatever Windows' default speakers are right now."""
        self.stream = self.sd.OutputStream(samplerate=self.rate, channels=1, dtype="float32",
                                           blocksize=self.block, callback=self._fill)
        self.stream.start()

    def close(self):
        self.stream.close()  # what's queued waits and plays once reopened

    def play(self, wav, sentence=""):
        from scipy.signal import resample_poly
        g = math.gcd(16000, self.rate)
        wav16 = resample_poly(wav, 16000 // g, self.rate // g).astype(np.float32)
        n = math.ceil(len(wav) / self.block)
        pad = lambda x, size: np.pad(x, (0, n * size - len(x)))[:n * size].reshape(n, size)
        self.chunks.extend((a, b, sentence) for a, b in zip(pad(wav, self.block), pad(wav16, 320)))

    def _fill(self, out, *_):
        chunk, chunk16, self.now = self.chunks.popleft() if self.chunks else (*self.silence, "")
        if self.now:
            self.sounded = time.monotonic()
        out[:, 0] = chunk
        self.last = chunk
        if self.on_play:
            self.on_play(chunk16)

    def busy(self):
        return bool(self.chunks)

    def just_spoke(self, within=4.0):
        """Speaking, or stopped a moment ago: a bare "Hey Jugnu" now is usually its own voice's
        tail, which Google bends toward the name it's been told to listen for."""
        return self.busy() or time.monotonic() - self.sounded < within

    def clear(self):
        self.chunks.clear()


class Google:
    """Google Cloud Text-to-Speech: one web request per sentence, nothing on the GPU."""

    def __init__(self, key, name=GOOGLE_VOICE):
        import requests
        self.http = requests.Session()  # keeps the connection open: no new handshake each sentence
        self.http.headers["X-Goog-Api-Key"] = key  # a header, so the key never shows up in an error's URL
        self.name = name

    def __call__(self, text):
        r = self.http.post("https://texttospeech.googleapis.com/v1/text:synthesize", timeout=(3, 10), json={
            "input": {"text": text},
            "voice": {"languageCode": "-".join(self.name.split("-")[:2]), "name": self.name},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": RATE}})
        if r.status_code != 200:
            try:
                why = r.json()["error"]["message"]
            except Exception:
                why = r.text[:200]
            raise RuntimeError(f"HTTP {r.status_code}: {why}")
        with wave.open(io.BytesIO(base64.b64decode(r.json()["audioContent"]))) as w:  # a WAV file
            pcm = w.readframes(w.getnframes())
        return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768


def free_gpu():
    """Hand back the GPU memory of a model just let go of."""
    import gc
    gc.collect()
    if "torch" in sys.modules:
        sys.modules["torch"].cuda.empty_cache()


class Chatterbox:
    """Chatterbox Turbo on the GPU, for private mode: nothing leaves the laptop."""

    def __init__(self):
        with quiet():
            import torch
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            from chatterbox.models.s3gen import flow_matching
            from chatterbox.models.t3 import t3

            # ponytail: patches Chatterbox's own progress bars and print, so the
            # per-sentence noise is gone without muting the whole console while it
            # talks. If an upgrade moves them, the noise comes back; nothing breaks.
            flow_matching.tqdm = t3.tqdm = lambda it, *a, **k: it
            flow_matching.print = lambda *a, **k: None
            self.model = ChatterboxTurboTTS.from_pretrained("cuda" if torch.cuda.is_available() else "cpu")
            if CLONE.exists():  # without it it still speaks, in Chatterbox's built-in voice
                self.model.prepare_conditionals(str(CLONE))
            self.model.generate("Ready.")  # warm-up: the first generate is the slow one

    def __call__(self, text):
        wav = self.model.generate(text).squeeze(0).cpu().numpy()
        if self.model.sr != RATE:
            from scipy.signal import resample_poly
            g = math.gcd(RATE, self.model.sr)
            wav = resample_poly(wav, RATE // g, self.model.sr // g).astype(np.float32)
        return wav


class Voice:
    def __init__(self, key):
        start = time.perf_counter()
        import sounddevice
        self.google, self.offline_until, self.down_since = Google(key) if key else None, 0.0, None
        self.local, self.private = None, False  # Chatterbox, while in private mode
        self.player = Player(sounddevice, RATE)
        self.caches = {False: {}, True: {}}  # sentence -> audio, for the fillers, per voice
        self.making = threading.Lock()     # one sentence at a time
        self.recent = collections.deque(maxlen=30)  # (when, what) it said lately: to spot its own echo
        if not self.google:
            print("voice: no GOOGLE_TTS_API_KEY in .env; silent unless in private mode")
        elif self.make("Ready.") is not None:  # checks the key, and opens the connection
            print(f"voice: Google {self.google.name}, in {time.perf_counter() - start:.1f}s")

    @property
    def cache(self):
        return self.caches[self.private]

    def go_private(self, on):
        """Private mode: Chatterbox on the GPU (loaded now, ~20 s) instead of Google; off unloads it."""
        if on and self.local is None:
            self.local = Chatterbox()
        with self.making:
            self.private = on
            if not on:
                self.local = None
        if not on:
            free_gpu()

    def make(self, text):
        """One sentence's audio, or None while Google can't be reached."""
        with self.making:
            if self.private:
                return self.local(text)
            if not self.google or time.monotonic() < self.offline_until:
                return None
            try:
                wav = self.google(text)
                self.down_since = None
                return wav
            except Exception as e:
                print(f"  (voice: Google failed: {e}; quiet for {OFFLINE}s, replies still show here."
                      " Private mode in the tray speaks without Google)", flush=True)
                self.offline_until = time.monotonic() + OFFLINE
                self.down_since = self.down_since or time.monotonic()

    def say(self, text, wait=True):
        """wait=False returns once the audio is queued, so the caller can make the next
        sentence while this one plays; say("") just waits for what's queued to finish."""
        self.stopped = False
        self.recent.append((time.monotonic(), text))
        for s in sentences(speakable(plain(text))):
            wav = self.cache.get(s)
            if wav is None:
                wav = self.make(s)
            if self.stopped:
                return
            if wav is None:  # Google down: the text is on screen anyway
                break
            self.player.play(wav, s)  # queued behind the last one: no gap between sentences
        while wait and self.player.busy() and not self.stopped:
            time.sleep(0.02)

    def prepare(self, lines, delay=0):
        """Record these once, in the background, so saying them later is instant."""
        time.sleep(delay)  # let the greeting go first
        for line in lines:
            for s in sentences(speakable(line)):
                if s not in self.cache and (wav := self.make(s)) is not None:
                    self.cache[s] = wav

    def stop(self):
        """Cut it off mid-sentence (called from another thread)."""
        self.stopped = True
        self.player.clear()


def load():
    """The Voice, or None if JUGNU_VOICE=off."""
    if os.getenv("JUGNU_VOICE", "on").lower() == "off":
        return None
    return Voice(os.getenv("GOOGLE_TTS_API_KEY"))


if __name__ == "__main__":
    assert sentences("Morning, Sam. Two things? Yes!  Done.") == \
        ["Morning, Sam.", "Two things?", "Yes!", "Done."]
    assert sentences("  ") == []
    assert plain("- **The three-read floor:** it counts reads, not lineages.") == "The three-read floor: it counts reads, not lineages."
    assert plain("## Findings\n1. Use `core/config.py`, see [the docs](http://x).") == "Findings\nUse core/config.py, see the docs."
    assert plain("Wi-Fi and 3-5 x 10^4 units") == "Wi-Fi and 3-5 x 10^4 units"
    real = sys.stdout  # two overlapping quiet()s, ending in the "wrong" order, still restore the console
    import threading as th
    a_in, b_in, a_out = th.Event(), th.Event(), th.Event()
    def a():
        with quiet():
            a_in.set(); b_in.wait()
        a_out.set()
    def b():
        a_in.wait()
        with quiet():
            b_in.set(); a_out.wait()
    ta, tb = th.Thread(target=a), th.Thread(target=b); ta.start(); tb.start(); ta.join(); tb.join()
    assert sys.stdout is real

    # Google's WAV comes back as floats; when Google fails it goes quiet, then tries again
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(np.array([0, 16384, -32768], np.int16).tobytes())
    reply = type("R", (), {"status_code": 200, "json": lambda self: {"audioContent": base64.b64encode(buf.getvalue()).decode()}})()
    g = Google.__new__(Google); g.name = GOOGLE_VOICE
    g.http = type("H", (), {"post": lambda self, url, **k: reply})()
    assert g("Hi.").tolist() == [0.0, 0.5, -1.0]
    calls = []
    def down(text):
        calls.append(text); raise RuntimeError("offline")
    v = Voice.__new__(Voice)
    v.google, v.offline_until, v.making, v.private, v.down_since = down, 0.0, threading.Lock(), False, None
    assert v.make("One.") is None and v.make("Two.") is None and calls == ["One."]  # tried once, then waits
    v.offline_until = 0.0; v.google = g
    assert v.make("Three.").tolist() == [0.0, 0.5, -1.0] and v.down_since is None  # back after the wait
    v.local, v.private = (lambda t: "chatterbox:" + t), True
    assert v.make("Four.") == "chatterbox:Four."  # private: never Google
    v.caches = {False: {"Hm.": "google"}, True: {}}
    assert "Hm." not in v.cache  # private has its own fillers, in its own voice
    print("ok")
