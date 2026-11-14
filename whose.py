"""Whose voice is this? So that only you can wake Jugnu and tell it what to do.

A small speaker model (WeSpeaker ResNet34, 26 MB, CC BY 4.0, trained on VoxCeleb) runs on this computer and
turns a stretch of speech into 256 numbers that sit close together for one person and far apart for two.
"Learn my voice" has you read a few lines; what it keeps is those numbers (logs/my_voice.npy), never a
recording. Nothing here leaves the computer. It keeps other people in the room from driving it; it is not a
lock (a recording of you would pass).

Run it for a self-check: python whose.py
"""
import os
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
RATE = 16000
MODEL = ROOT / "models" / "voice.onnx"  # fetched the first time your voice is learned
URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
       "wespeaker_en_voxceleb_resnet34.onnx")
MINE = ROOT / "logs" / "my_voice.npy"   # your voice as 256 numbers, then the bar a voice must clear to count as yours
# The bar (0-1) is set when your voice is learned, from how alike your own takes are to each other, and raised
# when you say "that wasn't me". On one laptop mic in one room, two people can score within 0.1 of each other
# (seen in use), so it is yours to tune: JUGNU_VOICE_MATCH fixes it by hand.
FIXED = os.getenv("JUGNU_VOICE_MATCH", "").strip()
SHORT, LENIENT, RECENT = 2.0, 0.05, 90  # under SHORT seconds, a little under the bar will do within RECENT seconds of a sure one
_session, _mine = None, None


def voiced(audio):
    """The speech in a clip without the quiet around it: the pause that ends what's said is as long as a short
    command, and with it in, everybody's clips sound like the same room."""
    audio = np.asarray(audio, np.float32)
    n = len(audio) // 160
    if n < 10:
        return audio
    frames = audio[:n * 160].reshape(n, 160)
    level = 10 * np.log10((frames ** 2).mean(1) + 1e-10)
    # speech is within 30 dB of the loudest of it, and well clear of the room's own level (the quietest tenth)
    return frames[level > max(np.percentile(level, 95) - 30, np.percentile(level, 10) + 8)].reshape(-1)


def fbank(audio):
    """Kaldi's 80 log mel filterbanks, as the model was trained on: 25 ms frames every 10 ms, the mean taken
    off. audio: float32, -1..1, 16 kHz. Checked against kaldi-native-fbank: the same to four decimal places."""
    x = np.asarray(audio, np.float64) * 32768
    n = 1 + (len(x) - 400) // 160
    if n < 1:
        return np.zeros((0, 80), np.float32)
    frames = x[np.arange(400)[None] + 160 * np.arange(n)[:, None]]
    frames = frames - frames.mean(1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], 1)  # pre-emphasis
    frames *= (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85  # Povey's window
    power = np.abs(np.fft.rfft(frames, 512)) ** 2
    mel = lambda f: 1127.0 * np.log(1 + f / 700.0)
    edges = np.linspace(mel(20.0), mel(8000.0), 82)
    bins = mel(np.arange(257) * RATE / 512)
    up = (bins[None] - edges[:-2, None]) / (edges[1:-1, None] - edges[:-2, None])
    down = (edges[2:, None] - bins[None]) / (edges[2:, None] - edges[1:-1, None])
    feats = np.log(np.maximum(power @ np.maximum(0, np.minimum(up, down)).T, np.finfo(np.float32).eps))
    return (feats - feats.mean(0)).astype(np.float32)


def embed(audio):
    """This speech as the model's 256 numbers, length 1; None if there's under 0.4 s of it."""
    global _session
    audio = None if audio is None else voiced(audio)
    if audio is None or len(audio) < 0.4 * RATE:
        return None
    if _session is None:
        if not MODEL.exists():  # once: 26 MB
            MODEL.parent.mkdir(exist_ok=True)
            part = MODEL.with_suffix(".part")
            urllib.request.urlretrieve(URL, part)
            part.replace(MODEL)
        import onnxruntime
        _session = onnxruntime.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    e = _session.run(None, {"feats": fbank(audio)[None]})[0][0]
    return e / (np.linalg.norm(e) or 1)


def known():
    return MINE.exists()


def learn(clips):
    """Learn your voice from what you read out. Returns the seconds of speech it learned from; ValueError if
    that's too little to go on. The bar starts just under how alike your own takes are to one another."""
    global _mine
    takes = [(e, len(voiced(c))) for c in clips for e in [embed(c)] if e is not None]
    seconds = sum(n for _, n in takes) / RATE
    if seconds < 5 or len(takes) < 5:
        raise ValueError("that was too little speech to learn a voice from")
    unit = lambda v: v / (np.linalg.norm(v) or 1)
    whole = sum(e * n for e, n in takes)
    own = [float(e @ unit(whole - e * n)) for e, n in takes]  # each take against your voice as learned from the others
    # well under your own takes: with the quiet trimmed off, other people score far lower (0.05 to 0.2 against
    # your 0.55 to 0.65, seen in use), and a bar set close to yours shuts you out on an off moment
    bar = float(np.clip(np.median(own) - 0.20, 0.35, 0.55))
    _mine = np.concatenate([unit(whole), [bar]]).astype(np.float32)
    MINE.parent.mkdir(exist_ok=True)
    np.save(MINE, _mine)
    return seconds


def forget():
    global _mine
    _mine = None
    MINE.unlink(missing_ok=True)


def _load():
    global _mine
    if _mine is None and known():
        _mine = np.load(MINE)
        if _mine.shape != (257,):  # learned by an earlier version: it has to be learned again
            _mine = None
            MINE.unlink(missing_ok=True)
    return _mine


def score(audio):
    """How like your voice this is, about 0 to 1; None if no voice has been learned or there's too little to tell."""
    e = embed(audio) if _load() is not None else None
    return None if e is None else float(_mine[:256] @ e)


def bar():
    """What a voice must score to count as yours."""
    return float(FIXED) if FIXED else float(_load()[256]) if _load() is not None else 1.0


def stricter(let_in):
    """"That wasn't me": something scoring let_in got through, so the bar goes just above it. Returns the new bar."""
    if _load() is not None and let_in is not None:
        _mine[256] = min(0.9, max(float(_mine[256]), let_in + 0.03))
        np.save(MINE, _mine)
    return bar()


def mine(how_alike, seconds, since_sure):
    """Is it you? how_alike: score(); seconds of speech; since_sure: seconds since something was surely yours.
    A word or two carries too little voice to be sure of, so soon after a sure one, just under the bar will do."""
    return how_alike >= bar() or (seconds < SHORT and since_sure < RECENT and how_alike >= bar() - LENIENT)


if __name__ == "__main__":
    t = np.arange(RATE) / RATE
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    f = fbank(tone)
    assert f.shape == (98, 80) and abs(float(f.mean())) < 1e-3 and np.isfinite(f).all(), f.shape
    assert fbank(tone[:100]).shape == (0, 80) and embed(tone[:100]) is None and embed(None) is None
    quiet = np.concatenate([np.zeros(RATE, np.float32), tone, np.zeros(RATE, np.float32)]) + 1e-4
    assert abs(len(voiced(quiet)) - RATE) < 800, len(voiced(quiet))  # the second of sound, without the two of quiet
    _mine = np.concatenate([np.ones(256) / 16, [0.60]]).astype(np.float32)  # a voice, with the bar at 0.60
    assert mine(0.66, 3.0, 999) and not mine(0.57, 3.0, 5) and mine(0.57, 1.0, 5) and not mine(0.57, 1.0, 999)
    assert not mine(0.50, 1.0, 5) and bar() == float(FIXED or np.float32(0.60))
    _mine = None
    try:
        learn([tone[:8000]])
        raise SystemExit("should have refused so little")
    except ValueError:
        pass
    print("ok")
