"""Listening test: the same Jugnu lines in different voices.

    uv run --python 3.12 --with kokoro --with soundfile --with pip voice_test.py kokoro
    uv run --python 3.12 --with chatterbox-tts --with soundfile voice_test.py chatterbox
    uv run --python 3.12 --with chatterbox-tts --with soundfile voice_test.py turbo

Writes .wav files to samples/ and prints how long each line took to make.
Each engine runs in its own throwaway environment, so nothing here touches
Jugnu's .venv.
"""
import sys
import time
from pathlib import Path

import soundfile as sf

OUT = Path(__file__).resolve().parent / "samples"

LINES = {
    "1_briefing": "Good morning. Two things need you today: the comments on "
                  "your draft, and the backup that finished overnight.",
    "2_technical": "The router hands out addresses over DHCP, and the laptop "
                   "keeps its lease for twenty-four hours before asking again.",
    "3_empathy": "Ah, that's frustrating. The third attempt failed again? "
                 "Let's look at what changed before we rerun it.",
    "4_dry_humour": "Done. And for the record, that's the fourth time this week "
                    "you've asked me what day it is.",
}


def save(name, wav, sr, seconds):
    OUT.mkdir(exist_ok=True)
    sf.write(OUT / f"{name}.wav", wav, sr)
    audio = len(wav) / sr
    print(f"  {name}: {seconds:.1f}s to make {audio:.1f}s of audio")


def kokoro():
    import numpy as np
    from kokoro import KPipeline

    # bm_ = British male, am_ = American male, af_ = American female
    for voice in ("bm_george", "am_michael", "af_heart"):
        pipe = KPipeline(lang_code=voice[0], repo_id="hexgrad/Kokoro-82M")
        print(f"kokoro {voice}")
        for key, text in LINES.items():
            t = time.perf_counter()
            wav = np.concatenate([a.numpy() for _, _, a in pipe(text, voice=voice)])
            save(f"kokoro_{voice}_{key}", wav, 24000, time.perf_counter() - t)


def _device():
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"running on {device} (torch {torch.__version__})")
    return device


def chatterbox():
    from chatterbox.tts import ChatterboxTTS

    model = ChatterboxTTS.from_pretrained(_device())
    print("chatterbox")
    for key, text in LINES.items():
        t = time.perf_counter()
        wav = model.generate(text)
        save(f"chatterbox_{key}", wav.squeeze(0).numpy(), model.sr, time.perf_counter() - t)
    # same empathetic line with the emotion dial turned up
    t = time.perf_counter()
    wav = model.generate(LINES["3_empathy"], exaggeration=0.9, cfg_weight=0.3)
    save("chatterbox_3_empathy_dialed_up", wav.squeeze(0).numpy(), model.sr, time.perf_counter() - t)


def turbo():
    # separate run: loading both Chatterbox models at once ran out of memory
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    turbo = ChatterboxTurboTTS.from_pretrained(_device())
    print("chatterbox turbo")
    for key, text in LINES.items():
        t = time.perf_counter()
        wav = turbo.generate(text)
        save(f"turbo_{key}", wav.squeeze(0).numpy(), turbo.sr, time.perf_counter() - t)
    # Turbo is meant to understand tags like [chuckle]; if you hear the word, it doesn't
    t = time.perf_counter()
    wav = turbo.generate("Done. [chuckle] And for the record, that's the fourth time "
                         "this week you've asked me what day it is.")
    save("turbo_4_dry_humour_tagged", wav.squeeze(0).numpy(), turbo.sr, time.perf_counter() - t)


def try_lines(texts, voice=None):
    """Render any lines in Turbo, e.g. to test spellings of a name.
    voice: a ~10-20s .wav of the speaker to sound like (default: Turbo's built-in voice)."""
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    model = ChatterboxTurboTTS.from_pretrained(_device())
    if voice:
        model.prepare_conditionals(voice)
        print(f"voice: {voice}")
    for i, text in enumerate(texts, 1):
        t = time.perf_counter()
        wav = model.generate(text)
        save(f"try_{i}", wav.squeeze(0).numpy(), model.sr, time.perf_counter() - t)
        print(f"    \"{text}\"")


if __name__ == "__main__":
    if sys.argv[1] == "try":
        args, voice = sys.argv[2:], None
        if args[:1] == ["--voice"]:
            voice, args = args[1], args[2:]
        try_lines(args, voice)
    else:
        {"kokoro": kokoro, "chatterbox": chatterbox, "turbo": turbo}[sys.argv[1]]()
    print(f"\nDone. Listen in: {OUT}")
