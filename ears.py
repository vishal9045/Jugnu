"""Jugnu's ears: an always-open mic, echo cancelling, a speech detector, and Google.

listen() runs in its own thread. The moment you start talking it opens a stream to
Google Speech-to-Text (Chirp 3) and sends your voice as you speak, so the words are
ready a moment after you stop; it hands them over with on_speech(text). The voice
tells it what the speakers are playing (played()), and WebRTC's echo canceller
subtracts that from the mic, so it hears you over itself: start talking while it's
speaking and on_interrupt fires.

The speech detector (Silero) runs here, on the CPU, so only your speech goes to
Google, never the silence in between. Asleep, Google hears just the first 2.5 s of
each thing said, enough for "Hey Jugnu"; the rest goes only if it was for it.
wake() checks text starts with "Hey Jugnu", sleep() whether you've sent it away.
Google is told to expect "Jugnu" and the terms in pronunciations.txt, so it
writes "SQL" rather than "sequel".

Private mode (the tray) loads Whisper large-v3-turbo on your GPU and hears you with
that instead: nothing goes to Google. Turning it off unloads Whisper.

Chirp 3 won't take an API key: it signs in as you, with the login saved once by
`gcloud auth application-default login`, and bills JUGNU_GOOGLE_PROJECT (the
Cloud project's ID).
python ears.py google : a live check. Set JUGNU_EARS=off in .env to type only.
"""
import collections
import concurrent.futures
import os
import queue
import re
import sys
import threading
import time
import types
import zlib
from pathlib import Path

import numpy as np

import pronounce
from speak import free_gpu, quiet

VAD_MODEL = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"  # Silero VAD v6 (MIT), 2 MB
RATE = 16000
TEN_MS = 160                       # the echo canceller works in 10 ms steps
FRAME = 512                        # Silero VAD's frame: 32 ms
END = int(2.0 * RATE / FRAME)      # this much quiet means you've finished (room to think)...
SOON = int(float(os.getenv("JUGNU_PAUSE", "1.0")) * RATE / FRAME)  # ...but after this much, if what you said sounds finished, answer now
LONGEST = int(60 * RATE / FRAME)   # a minute of nonstop "speech" is a video or a fan: cut it there
WAKE_WINDOW = int(2.5 * RATE / FRAME)  # asleep, Google hears this much of each thing said
REGION = os.getenv("JUGNU_STT_REGION", "eu")  # eu or us (Chirp 3's regions)
# How soon Google decides a pause ends what you said: standard, short or supershort.
# Sooner means the words are ready while Jugnu is still waiting out your pause.
ENDPOINTING = os.getenv("JUGNU_STT_ENDPOINTING", "supershort")  # the fastest when tested; words as good
# The two knobs for a quiet voice or a far mic, set in .env:
HEAR = float(os.getenv("JUGNU_HEAR_THRESHOLD", "0.35"))  # how sure the voice detector must be that you've
                                                           # started talking: lower hears softer speech (0.3-0.5)
GAIN = float(os.getenv("JUGNU_MIC_GAIN", "1.5"))         # boosts the mic before anything else (1.0-4.0)
# How hard Google is nudged towards its name and your word list (0-20). At 10 it wrote the name onto the end of
# things nobody said it in, yours and other people's (seen in use); lower it if that comes back, raise it if the
# name stops being heard.
BOOST = float(os.getenv("JUGNU_HINT_BOOST", "4"))
# Its own name is hinted: without it Google wrote a bare "Hey Jugnu" as "pay your loan" and "age of no" (seen in
# use). The cost is a stray "Hey Jugnu" on the end of some sentences; set JUGNU_NAME_HINTS= (empty) to trade back.
NAME_HINTS = [h.strip() for h in os.getenv("JUGNU_NAME_HINTS", "Jugnu,Hey Jugnu").split(",") if h.strip()]
BARGE = 12                         # ~0.4 s of clear speech over it = you're interrupting (shorter let
                                   # stray echo of its own voice stop it mid-sentence)
LEARN = float(os.getenv("JUGNU_ECHO_LEARN", "6"))  # seconds of its voice the echo canceller gets to learn the room
                                   # before you can talk over it (at 2 its own first answers stopped it: seen in use)

# How its name gets heard. Add misses here when you see them: the three patterns below all use this list.
NAME = r"(?:jugnu|jugnoo|jugno|juggnu|joognu|jagnu|jug\W?(?:new|nu|noo|knew))"
WAKE = re.compile(r"^\W*(?:(?:hey|ey|hi|hello|okay|ok|a)\W+)?" + NAME + r"\b\W*(.*)", re.I | re.S)

# A whole-utterance send-off; up to four words in front ("And then go home", where
# "Jugnu" was misheard; "I wanted to go to sleep"), not counting an opening "Okay," or "Right,".
SLEEP = re.compile(
    r"\W*(?:(?:okay|ok|alright|right|so|well|yeah|yes|now)\W+)*(?:[\w']+\W+){0,4}"
    r"(?:stop listening|go(?:t)? to sleep(?:\W+mode)?|go home|sleep|that'?s all|that is all|good ?bye|bye|good night)"
    r"(?:\W+(?:now|for now|jugnu|thanks|thank you|please|then|okay|ok|hey|boss|buddy|yaar|andy|jugnoo))*\W*",
    re.I)


def sleep(text):
    """True if it's a send-off like "Okay, go to sleep." (not "how much sleep..."), on its own
    or as the last sentence: "Yeah, I think it's okay for now. Jugnu, go to sleep." """
    last = re.split(r"(?<=[.!?])\s+", text.strip())[-1]
    return bool(SLEEP.fullmatch(text) or SLEEP.fullmatch(last))


# "Log everything", "let's start fresh", "log this and start a new conversation": a few words
# around them are fine, but not a question ("did you log it?"), a no, or a story ("I had a new
# conversation with Sam").
_CMD_LEAD = (r"(?!\W*(?:is|are|was|what|why|how|does|do|did|when|where|who|can't)\b)"
             r"\W*(?:(?!(?:don'?t|not|never|no|had|have|was|were|in|about|with|my|his|her|their)\b)[\w']+\W+){0,5}?")
_CMD_TAIL = r"(?:\W+[\w']+){0,6}\W*"
FRESH = re.compile(_CMD_LEAD + r"(?:start(?:ing)? (?:a )?(?:fresh|afresh|over|anew|(?:a )?new (?:conversation|chat|session))"
                   r"|(?:make|open|begin|have) a (?:new|fresh) (?:conversation|chat|session)|new (?:conversation|chat|session)"
                   r"|fresh (?:start|conversation|chat|session)|clean slate|reset (?:the |this |our )?(?:conversation|chat))"
                   + _CMD_TAIL, re.I)
LOG = re.compile(_CMD_LEAD + r"(?:log (?:everything|it all|all (?:of )?(?:it|this)|(?:this|the|our) (?:conversation|chat|session))"
                 r"|save (?:this|the|our) (?:conversation|chat|session)|keep a (?:log|record) of (?:this|everything|it))"
                 + _CMD_TAIL, re.I)


def conversation(text):
    """["log"], ["fresh"] or ["log", "fresh"] when that's all that was asked for; [] otherwise (anything
    more in it, and it goes to Claude whole)."""
    if len(text.split()) > 20:
        return []
    acts = []
    for part in re.split(r"[.!?;]+|,?\s+(?:and then|and|then)\s+", text):
        if LOG.fullmatch(part):
            acts.append("log")
        elif FRESH.fullmatch(part):
            acts.append("fresh")
        elif len(re.findall(r"[\w']+", part)) > 2:  # something else asked too
            return []
    return [a for a in ("log", "fresh") if a in acts]


# "Go private" / "switch back to online mode": a whole sentence, up to five words in front
# ("Now I want you to ..."), but not a question about it ("Is private mode on?").
_LEAD = (r"(?!\W*(?:is|are|was|what|why|how|does|do|did|when|where|tell)\b)"
         r"\W*(?:(?!(?:don'?t|not|never|no)\b)[\w']+\W+){0,5}?")  # "I don't want private mode" isn't a yes
_END = r"(?:\W+(?:please|now|jugnu|thanks|thank you))*\W*"
_PRIVATE = r"(?:the\W+)?private(?:\W+mode)?"
_ONLINE = r"(?:the\W+)?(?:online|normal|regular|google)(?:\W+mode)?"
PRIVATE_ON = re.compile(_LEAD + r"(?:go private|private mode on|(?:go|switch|turn) offline"
                        r"|(?:turn on|switch on|switch to|switch over to|go to|go into|enter|start|use|activate)\W+"
                        + _PRIVATE + ")" + _END, re.I)
PRIVATE_OFF = re.compile(_LEAD + r"(?:private mode off|go online"
                         r"|(?:turn off|switch off|leave|exit|stop|end|disable|come out of|get out of)\W+" + _PRIVATE +
                         r"|(?:(?:go|switch|come|get)\W+)?back\W+(?:to\W+)?" + _ONLINE +
                         r"|(?:go|switch|turn)\W+(?:over\W+)?(?:to\W+)?" + _ONLINE + ")" + _END, re.I)


# Learning mode ("switch on the learning mode"): you read out the lines it shows, and whatever Google wrote
# instead goes into heard.txt as  heard = meant. From then on the commands are matched on what you meant.
HEARD_FILE = Path(__file__).resolve().parent / "heard.txt"
_LEARNING = r"(?:the\W+)?(?:learning|training|practice|practise)(?:\W+(?:mode|session))?"
LEARN_ON = re.compile(_LEAD + r"(?:(?:turn on|switch on|switch to|go to|go into|enter|start|begin|activate)\W+" + _LEARNING
                      + r"|" + _LEARNING + r"\W+on|let'?s\W+practi[cs]e)" + _END, re.I)
LEARN_OFF = re.compile(_LEAD + r"(?:(?:turn off|switch off|leave|exit|stop|end|finish|quit)\W+" + _LEARNING
                       + r"|" + _LEARNING + r"\W+off)" + _END, re.I)
# Vision: "turn your vision on", "look at my screen"; "turn your vision off", "stop looking"
_VISION = r"(?:your\W+|the\W+)?(?:vision|eyes)(?:\W+mode)?"
_LOOK = r"(?:look|watch|see)\W+(?:at\W+)?(?:my|the|this)\W+screen\b(?!\W*(?:shot|recording|saver))"
VISION_ON = re.compile(_LEAD + r"(?:(?:turn on|switch on|open|start)\W+" + _VISION + r"|(?:(?:turn|switch)\W+)?" + _VISION
                       + r"\W+on|" + _LOOK + r")" + _END, re.I)
VISION_OFF = re.compile(_LEAD + r"(?:(?:turn off|switch off|close|stop)\W+" + _VISION + r"|(?:(?:turn|switch)\W+)?" + _VISION
                        + r"\W+off|stop\W+(?:looking|watching)(?:\W+(?:at\W+)?(?:my|the|this)\W+screen)?)" + _END, re.I)
# "Look at my screen and tell me what's wrong": vision comes on and the rest is still the question
LOOK = re.compile(r"\W*(?:(?:hey|hi|okay|ok)\W+)?(?:" + NAME + r"\W+)?(?:(?:can|could|will|would)\W+you\W+|please\W+)*" + _LOOK, re.I)
LEARN_STOP = re.compile(r"\W*(?:stop|cancel|that'?s enough|enough|done|we'?re done|finish)\W*", re.I)  # during a session
LEARN_SKIP = re.compile(r"(?:.*\W)?(?:skip|skip it|skip this|next|pass)\W*", re.I | re.S)  # "SQL... skip"
LEARN_BACK = re.compile(r"(?:\W*(?:go\W+)?(?:back|previous)(?:\W+one)?\W*)+", re.I)  # "Go back. Go back."
LEARN_OWN = re.compile(r"\b(?:words?|terms?|names?|own|new|something else|teach)\b", re.I)  # "a word of my own"


def learning(text):
    """True for "switch on the learning mode", False for "learning mode off", None for anything else."""
    return True if LEARN_ON.fullmatch(text) else False if LEARN_OFF.fullmatch(text) else None


# The browser's window: "show the browser", "hide the browser"; said too as headless mode off / on
_BROWSER = r"(?:the\W+|your\W+|that\W+)?browser(?:\W+window)?"
_HEADLESS = r"(?:the\W+)?head\W*less(?:\W+mode)?"
BROWSER_SHOW = re.compile(_LEAD + r"(?:(?:show(?:\W+me)?|unhide|bring\W+(?:up|out|back))\W+" + _BROWSER
                          + r"|bring\W+" + _BROWSER + r"\W+(?:up|out|back|(?:to\W+)?(?:the\W+)?(?:front|foreground))"
                          + r"|(?:turn|switch)\W+off\W+" + _HEADLESS + r"|(?:(?:turn|switch)\W+)?" + _HEADLESS + r"\W+off)" + _END, re.I)
BROWSER_HIDE = re.compile(_LEAD + r"(?:(?:hide|put\W+away)\W+" + _BROWSER + r"|put\W+" + _BROWSER + r"\W+away"
                          + r"|(?:run|keep|put)\W+" + _BROWSER + r"\W+in\W+(?:the\W+)?background"
                          + r"|(?:turn|switch)\W+on\W+" + _HEADLESS + r"|(?:(?:turn|switch)\W+)?" + _HEADLESS + r"\W+on)" + _END, re.I)


def browser_window(text):
    """True for "show the browser", False for "hide the browser", None for anything else."""
    return True if BROWSER_SHOW.fullmatch(text) else False if BROWSER_HIDE.fullmatch(text) else None


def vision(text):
    """True for "turn your vision on" or "look at my screen", False for "turn your vision off", None for anything else."""
    return True if VISION_ON.fullmatch(text) else False if VISION_OFF.fullmatch(text) else None


# Whose voice: "learn my voice", "only listen to me", "listen to everyone", "forget my voice" (whose.py)
VOICE_LEARN = re.compile(_LEAD + r"(?:learn|remember|recogni[sz]e|know)\W+(?:only\W+)?my\W+voice(?:\W+only)?" + _END, re.I)
VOICE_ONLY = re.compile(_LEAD + r"(?:(?:only\W+)?listen\W+(?:only\W+)?to\W+(?:me|my\W+voice)(?:\W+only)?|"
                        r"(?:turn\W+on|switch\W+on)\W+(?:the\W+)?voice\W+lock|(?:the\W+)?voice\W+lock\W+on)" + _END, re.I)
VOICE_ALL = re.compile(_LEAD + r"(?:listen\W+to\W+(?:everyone|everybody|anyone|anybody)|"
                       r"(?:turn\W+off|switch\W+off)\W+(?:the\W+)?voice\W+lock|(?:the\W+)?voice\W+lock\W+off)" + _END, re.I)
VOICE_NOT_ME = re.compile(_LEAD + r"(?:that\W+(?:was\W*n'?t|was\W+not|is\W*n'?t|is\W+not)\W+(?:me|my\W+voice)|"
                          r"that\W+was\W+(?:someone|somebody)\W+else(?:'s\W+voice)?)" + _END, re.I)
VOICE_FORGET = re.compile(_LEAD + r"forget\W+my\W+voice" + _END, re.I)


def whose_voice(text):
    """"learn" for "learn my voice", True for "only listen to me", False for "listen to everyone", "forget" for
    "forget my voice", None for anything else."""
    if VOICE_NOT_ME.fullmatch(text):  # "that wasn't me": be stricter
        return "stricter"
    return ("learn" if VOICE_LEARN.fullmatch(text) else "forget" if VOICE_FORGET.fullmatch(text) else
            True if VOICE_ONLY.fullmatch(text) else False if VOICE_ALL.fullmatch(text) else None)


def load_heard():
    """heard.txt as {heard: meant}; lines starting with # are notes."""
    try:
        lines = HEARD_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    pairs = (line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))
    return {a.strip().lower(): b.strip() for a, b in pairs if a.strip() and b.strip()}


HEARD, _heard_at = {}, [None]


def heard():
    """heard.txt, read again whenever it changes: learning mode writes it, and so can the brain or you."""
    try:
        at = HEARD_FILE.stat().st_mtime
    except OSError:
        at = 0
    if at != _heard_at[0]:
        _heard_at[0] = at
        HEARD.clear()
        HEARD.update(load_heard())
    return HEARD


heard()


def heard_as(text, names=False):
    """text with what was learned put right ("sonet" -> "Sonnet"). names: only mishearings of its own name,
    which are safe anywhere. The rest are for matching commands: an ordinary word that once stood in for a
    model's name ("rock" for Grok) mustn't be rewritten in what you go on to ask."""
    for was, meant in sorted(heard().items(), key=lambda kv: -len(kv[0])):
        if not names or re.search(NAME, meant, re.I):
            text = re.sub(r"(?<!\w)" + r"\W+".join(map(re.escape, was.split())) + r"(?!\w)", meant, text, flags=re.I)
    return text


def lesson(expected, heard):
    """What one take teaches: [] if it came through as written, [(heard, meant), ...] for the words Google wrote
    differently, None if it was too far off to be a take at all (a cough, some other sentence)."""
    import difflib
    close = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
    ew, hw = re.findall(r"[\w'-]+", expected), re.findall(r"[\w'-]+", heard.lower())
    el = [w.lower() for w in ew]
    if hw == el:
        return []
    if close(" ".join(hw), " ".join(el)) < 0.5:
        return None
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, hw, el).get_opcodes():
        if tag == "replace":
            if close("".join(hw[i1:i2]), "".join(el[j1:j2])) < 0.4:
                return None
            out.append((" ".join(hw[i1:i2]), " ".join(ew[j1:j2])))
    return out


def learn(pairs):
    """Add what a session found to heard.txt, and to this run."""
    heard().update({a.lower(): b for a, b in pairs})
    HEARD_FILE.write_text("# What Jugnu hears, and what you meant. One per line:  heard = meant\n"
                          "# Learning mode adds to this; edit or delete lines freely.\n\n"
                          + "".join(f"{a} = {b}\n" for a, b in HEARD.items()), encoding="utf-8")


# Self-improve mode: "apply the change" swaps a checked copy of its program in; "undo the last change" puts
# the old files back. Whole sentences only, so "apply the change to the figure legend" isn't one.
APPLY = re.compile(r"\W*(?:(?:yes|okay|ok|please|now|go ahead and)\W+)*apply (?:it|that|the (?:change|fix|update)s?)" + _END, re.I)
UNDO = re.compile(r"\W*(?:(?:okay|ok|please|now)\W+)*(?:undo|revert|roll back) (?:it|that|(?:the|that|your) (?:last )?"
                  r"(?:change|fix|update|self-improvement)s?)" + _END, re.I)


def private(text):
    """True for "go private", False for "back to online mode", None for anything else. Like
    sleep(), it can be the last sentence: "OK, it's working. Now switch back to online mode." """
    for t in (text, re.split(r"(?<=[.!?])\s+", text.strip())[-1]):
        if PRIVATE_ON.fullmatch(t):
            return True
        if PRIVATE_OFF.fullmatch(t):
            return False
    return None


# Said while it's working: drop the job. Only as a sentence of its own ("Stop." or "Cancel
# that, ..."), so "stop codon in exon 3" isn't one.
STOP = re.compile(r"\W*(?:(?:hey\W+)?jugnu\W+)?(?:(?:i\W+think\W+)?(?:you\W+should|please|just|can\W+you|could\W+you)\W+)?"
                  r"(?:stop(?: (?:that|it|working|now|there))?|cancel(?: (?:that|it|this|(?:this|that|the) (?:job|task)))?"
                  r"|never ?mind|forget (?:it|that)|abort|leave it)(?:\W+jugnu)?\s*(?:[.!,;]+|$)\s*", re.I)


def stop(text):
    """For "Stop. Read the other folder instead.": the words after the stop; None if it isn't one."""
    m = STOP.match(text)
    return text[m.end():].strip() if m else None


TRAILING_WAKE = re.compile(r"[\s,]+\b(?:hey|hi|okay|ok)\W+" + NAME + r"\W*$", re.I)


_CALLED = re.compile(r"\b(?:hey|hi|okay|ok)\W+" + NAME + r"\b[.,!?]*", re.I)


def tidy(text):
    """Calls to it left inside what was said ("Gemini seems slow. Hey, Jugnu.") taken out."""
    text = heard_as(text, names=True)
    return re.sub(r"\s{2,}", " ", _CALLED.sub("", text)).strip(" ,")


def wake(text):
    """'Hey Jugnu, what's X?' -> "what's X?"; just its name -> ""; else None. Said twice
    ("Hey Jugnu. Hey Jugnu.") or tacked on the end ("Can you do that? Hey Jugnu."),
    the extra calls are dropped too."""
    m = WAKE.match(heard_as(text, names=True))
    if not m:
        return None
    rest = m.group(1).strip()
    while (again := WAKE.match(rest)) and again.group(1).strip() != rest:
        rest = again.group(1).strip()
    return TRAILING_WAKE.sub("", rest).strip()


# What speech recognisers invent from a short burst of noise or echo. Heard alone in a short clip,
# they're dropped: a phantom "Bye." once sent it to sleep while nobody spoke.
PHANTOMS = {"bye", "goodbye", "bye bye", "thank you", "thanks", "thank you very much", "thanks for watching",
            "thank you for watching", "you", "so", "um", "uh", "hmm"}  # not "yeah"/"okay": real answers to its questions


def phantom(text, seconds):
    return seconds < 1.5 and re.sub(r"[^\w ]", "", text.lower()).strip() in PHANTOMS


# a sentence ending on one of these isn't finished, whatever the full stop says
TRAILING = {"and", "but", "so", "or", "because", "um", "uh", "er", "hmm", "like", "the", "a", "an",
            "to", "of", "with", "that", "which", "then", "if", "is", "my", "your", "i", "we", "about",
            "for", "in", "on", "at", "maybe", "also", "actually", "basically", "just"}


def sounds_finished(text):
    """Does this sound like the end of what you wanted to say? A full stop or question mark
    on a word that can end a sentence: yes. A comma, dots, or "and", "so", "um": keep waiting."""
    t = text.strip()
    if not t or t[-1] not in ".?!" or t.endswith(("...", "..")):
        return False
    words = re.findall(r"[\w']+", t.lower())
    return bool(words) and words[-1] not in TRAILING


def echo(text, recent, within=15):
    """Is this its own voice coming back? recent: (time.monotonic(), sentence) it said lately.
    A short one has to be one of its sentences ("Right."); a longer one, nearly all
    its words must be ones it said ("Let me see. Thanks Sam."). Google is nudged towards its name,
    so an echo can come back with a "Hey Jugnu" it never said tacked on: that part isn't counted."""
    norm = lambda t: re.findall(r"[a-z0-9']+", _CALLED.sub(" ", t).lower())
    lately = [said for at, said in recent if time.monotonic() - at < within]
    heard = norm(text)
    if not heard or not lately:
        return False
    own = [norm(s) for said in lately for s in re.split(r"(?<=[.!?])\s+", said)]
    if sorted(heard) in [sorted(s) for s in own]:  # one of its sentences, maybe jumbled ("Sam Hey.")
        return True
    words = {w for s in own for w in s}
    return len(heard) >= 3 and sum(w in words for w in heard) >= len(heard) - (len(heard) >= 6)


def looping(text):
    """A recogniser's failure on noise: one phrase over and over. Long loops compress far
    better than speech (OpenAI's Whisper cutoff); short ones reuse a few words."""
    words = re.findall(r"[\w-]+", text.lower())
    if len(words) >= 6 and len(set(words)) <= len(words) / 2:
        return True
    return len(text) > 40 and len(text) / len(zlib.compress(text.encode())) > 2.4


class Vad:
    """Silero's speech detector run with onnxruntime: the same model, without torch (whose
    import alone took seconds). Gives how sure it is (0-1) that a 32 ms frame is speech."""

    def __init__(self):
        import onnxruntime
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(str(VAD_MODEL), opts,
                                                    providers=["CPUExecutionProvider"])
        self.reset_states()

    def reset_states(self):
        self.state, self.context = np.zeros((2, 1, 128), np.float32), np.zeros((1, 64), np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.context, frame[None].astype(np.float32)], axis=1)  # each frame sees the last 64 samples
        out, self.state = self.session.run(None, {"input": x, "state": self.state, "sr": np.array(RATE, np.int64)})
        self.context = x[:, -64:]
        return float(out[0, 0])


class Google:
    """Google Speech-to-Text, Chirp 3, streamed. open() starts one utterance's stream."""

    def __init__(self, phrases):
        from google.api_core.client_options import ClientOptions
        from google.cloud.speech_v2 import SpeechClient
        from google.cloud.speech_v2.types import cloud_speech

        self.cs = cs = cloud_speech
        project = os.getenv("JUGNU_GOOGLE_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("set JUGNU_GOOGLE_PROJECT in .env (your Cloud project's ID)")
        try:  # your gcloud login (Speech-to-Text v2 refuses API keys)
            self.client = SpeechClient(client_options=ClientOptions(
                api_endpoint=f"{REGION}-speech.googleapis.com", quota_project_id=project))
        except Exception as e:
            if "DefaultCredentials" not in type(e).__name__:
                raise
            raise RuntimeError("no Google login on this computer: run "
                               "`gcloud auth application-default login` once") from None
        self.request = lambda hints, ending=ENDPOINTING: cs.StreamingRecognizeRequest(
            recognizer=f"projects/{project}/locations/{REGION}/recognizers/_",
            streaming_config=cs.StreamingRecognitionConfig(streaming_features=cs.StreamingRecognitionFeatures(
                endpointing_sensitivity=f"ENDPOINTING_SENSITIVITY_{ending.upper()}"), config=cs.RecognitionConfig(
                explicit_decoding_config=cs.ExplicitDecodingConfig(
                    encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=RATE, audio_channel_count=1),
                language_codes=[os.getenv("JUGNU_STT_LANGUAGE", "en-IN")], model="chirp_3",
                features=cs.RecognitionFeatures(enable_automatic_punctuation=True),
                adaptation=cs.SpeechAdaptation(phrase_sets=[cs.SpeechAdaptation.AdaptationPhraseSet(
                    inline_phrase_set=cs.PhraseSet(phrases=[cs.PhraseSet.Phrase(value=p, boost=BOOST) for p in phrases]))])
                if hints and phrases else None)))  # nothing to hint: no hints sent
        self.first = self.request(True)
        self.complained, self.down_since = 0.0, None

    def check(self):
        """At startup: one short stream, to see the key works (and open the connection)."""
        s = None
        for hints in (True, True, False):  # if Google won't take the hints, go without them...
            if s and hints and not isinstance(s.error, TimeoutError):
                continue  # ...but a slow first connection (seen in use) isn't a no: the hints get a second try
            self.first = self.request(hints)
            s = self.open()
            s.push(np.zeros(RATE // 2, np.float32))
            s.finish(timeout=8.0)
            if s.error is None:
                return "" if hints else " (without word hints)"
        raise RuntimeError(str(s.error).splitlines()[0][:300])

    def open(self):
        return Stream(self)

    def failed(self, error):
        self.down_since = self.down_since or time.monotonic()
        if time.monotonic() - self.complained > 30:  # say so, but not every time
            print(f"  (ears: Google failed: {str(error).splitlines()[0][:200]}."
                  " Private mode in the tray hears you without Google)", flush=True)
            self.complained = time.monotonic()


class Stream:
    """One utterance on its way to Google: push() audio as it comes, finish() for the words."""

    def __init__(self, google):
        self.google, self.chunks, self.finals = google, queue.Queue(), []
        self.waiting, self.error, self.done = np.zeros(0, np.float32), None, threading.Event()
        self.final_at = None  # when the last words came back
        self.sent = self.spoke = self.heard_upto = 0  # samples sent; up to the last speech; Google's done up to
        threading.Thread(target=self._run, daemon=True).start()

    def _requests(self):
        yield self.google.first
        while not self.done.is_set():
            try:
                data = self.chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                return  # tells Google that's all: it sends the last words and closes
            yield self.google.cs.StreamingRecognizeRequest(audio=data)

    def _run(self):
        try:
            for response in self.google.client.streaming_recognize(requests=self._requests()):
                for result in response.results:
                    if result.is_final and result.alternatives:
                        self.finals.append(result.alternatives[0].transcript.strip())
                        self.final_at = time.perf_counter()
                        self.heard_upto = int(result.result_end_offset.total_seconds() * RATE)
        except Exception as e:  # no internet, a bad key: the caller hears nothing
            self.error = e
        finally:
            self.done.set()

    def push(self, audio, speech=True):
        self.sent += len(audio)
        if speech:
            self.spoke = self.sent
        self.waiting = np.concatenate([self.waiting, audio])
        if len(self.waiting) >= RATE // 10:  # about 100 ms a message, as Google suggests
            self._send()

    def _send(self):
        if len(self.waiting):
            self.chunks.put((np.clip(self.waiting, -1, 1) * 32767).astype(np.int16).tobytes())
            self.waiting = np.zeros(0, np.float32)

    def close(self):
        self._send()
        self.chunks.put(None)

    def finish(self, timeout=4.0):
        """The words, a moment after the last audio (empty if Google failed). If Google has
        already finished everything up to your last word (it does during your pause), at once."""
        self.close()
        if self.finals and self.heard_upto >= self.spoke - RATE // 10:
            self.google.down_since = None
            return " ".join(f for f in self.finals if f)  # the stream closes in the background
        if not self.done.wait(timeout):
            self.error = self.error or TimeoutError("no reply in time")
        if self.error:
            self.google.failed(self.error)
        else:
            self.google.down_since = None
        return " ".join(f for f in self.finals if f)


class Whisper:
    """Whisper large-v3-turbo on the GPU, for private mode. Primed with "Jugnu" and the
    terms in pronunciations.txt, so it writes "SQL" rather than "sequel"."""

    def __init__(self, terms):
        self.lock, self.terms = threading.Lock(), terms
        with quiet():
            import torch
            from transformers import logging, pipeline

            logging.set_verbosity_error()
            cuda = torch.cuda.is_available()
            self.asr = pipeline("automatic-speech-recognition", model=os.getenv("JUGNU_WHISPER", "openai/whisper-large-v3-turbo"),
                                torch_dtype=torch.float16 if cuda else torch.float32,
                                device="cuda" if cuda else "cpu")
            self.hint = self.asr.tokenizer.get_prompt_ids(
                f"Hey Jugnu. {', '.join(terms)}.", return_tensors="pt").to(self.asr.device)
            self(np.random.default_rng(0).normal(0, 0.1, RATE).astype(np.float32))  # warm-up

    def __call__(self, audio):
        audio = audio[:int(29.5 * RATE)]  # past 30 s Whisper refuses outright
        if len(audio) < 0.3 * RATE:
            return ""
        try:
            with self.lock:
                out = self.asr({"raw": audio, "sampling_rate": RATE}, generate_kwargs={
                    "language": "english", "task": "transcribe", "prompt_ids": self.hint,
                    "max_new_tokens": int(8 * len(audio) / RATE) + 10})  # nobody says 8 tokens a second
            return self.unprimed(out["text"].strip())
        except Exception as e:  # one bad clip mustn't take Jugnu down
            print(f"  (couldn't transcribe that: {str(e)[:120]})", flush=True)
            return ""


    def unprimed(self, text):
        """Whisper sometimes starts by reciting the terms it was primed with ("Wi-Fy, SQL, CARD,
        PubMed, Jugnoo Are you still..."): drop a run of two or more of them from the front."""
        words = text.split()
        primed = lambda w: len(w.strip(",.").strip()) >= 3 and any(
            t.lower().startswith(w.strip(",.").lower()[:3]) for t in self.terms)
        n = 0
        while n < len(words) and primed(words[n]):
            n += 1
        return " ".join(words[n:]) if n >= 2 else text


class Local:
    """Stands in for a Google stream: keeps the audio, and hears it all at finish()."""

    def __init__(self, hear):
        self.hear, self.audio, self.error = hear, [], None

    def push(self, audio, speech=True):
        self.audio.append(audio)

    def close(self):
        pass

    def finish(self):
        return self.hear(np.concatenate(self.audio)) if self.audio else ""


class Ears:
    def __init__(self):
        print("loading ears...")
        start = time.perf_counter()
        import sounddevice
        from livekit import rtc

        self.sd, self.rtc = sounddevice, rtc
        self.vad = Vad()
        self.aec = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True,
                                             high_pass_filter=True, auto_gain_control=True)
        self.terms = list(pronounce.load())
        self.google, self.whisper, self.private = None, None, False
        try:
            if os.getenv("JUGNU_SENSES", "google").lower() == "local":  # set up local-only: never Google
                raise RuntimeError("set up to use Whisper on this computer, not Google")
            self.google = Google([*NAME_HINTS, *self.terms])
            where = f"Google Chirp 3, {REGION}{self.google.check()}"
        except Exception as e:  # kept if it was made: it recovers when the internet's back
            where = f"Google unreachable: {str(e).splitlines()[0][:200]}. Private mode in the tray hears you without it"
        self.render = collections.deque(maxlen=500)  # what the speakers played, 20 ms chunks (10 s:
                                                     # room to catch up without losing their place)
        self.learned = 0.0                          # seconds of its voice the canceller has had
        self.voiced = 0.0                           # when its voice last came out of the speakers
        self.over = False                           # did the latest thing heard begin while it spoke, or just after?
        self.clip = None                            # the sound of the latest thing heard: to tell whose voice it was
        self.talking = lambda: False                # set by jugnu.py: is it speaking now?
        self.awake = lambda: True                   # set by jugnu.py: is it listening to everything?
        self.level = 0.0                            # your voice's loudness, for the face
        self.loud = 0                               # frames of you talking over it, so far
        self.switching, self.closed, self.go = threading.Event(), threading.Event(), threading.Event()
        print(f"ears on ({where}) in {time.perf_counter() - start:.1f}s")

    def retune(self):
        """Your word list changed: Google is told to expect the new terms, from the next thing said."""
        self.terms = list(pronounce.load())
        if self.google:
            try:
                self.google = Google([*NAME_HINTS, *self.terms])
            except Exception:  # noqa: BLE001  (offline: the old list stays until next time)
                pass

    def open(self):
        """A stream for the next thing said: Whisper's in private mode, else Google's."""
        if self.private:
            return Local(self.whisper)
        return self.google.open() if self.google else Local(lambda audio: "")

    def go_private(self, on):
        """Private mode: Whisper on the GPU (loaded now, ~20 s) instead of Google; off unloads it."""
        if on and self.whisper is None:
            self.whisper = Whisper(self.terms)
        self.private = on
        if not on:
            self.whisper = None
            free_gpu()

    def words(self, text, audio):
        """The text worth handing over: not from silence, a cough, or noise."""
        seconds = len(audio) / RATE
        if seconds < 0.3:
            return ""
        if np.sqrt(np.mean(audio ** 2)) < 0.0015:
            if seconds > 1:  # a second or more of something: say so, so a missed sentence shows in the log
                print("  (heard something, too faint to make out)", flush=True)
            return ""
        words = re.findall(r"[\w-]+", text)
        echoed = lambda w: any(t.startswith(w) for t in self.terms)  # cut-off "Wi-" counts
        if looping(text) or (words and all(echoed(w) for w in words)):
            return ""  # a loop, or just the hinted terms echoed back from noise
        if phantom(text, seconds):
            print(f"  (ignored a likely phantom: {text})", flush=True)
            return ""
        return text.strip()

    def played(self, chunk):
        """The voice calls this with each 20 ms (16 kHz) it sends to the speakers."""
        self.render.append(chunk)
        if np.abs(chunk).max() > 0.01:
            self.learned += 0.02
            self.voiced = time.monotonic()

    def listen(self, on_speech, on_interrupt=lambda: None):
        """Forever: on_speech(audio) each time someone finishes speaking, and
        on_interrupt() the moment you start talking over it. pause()/resume()
        let follow_devices() swap the mic underneath."""
        last = None
        while True:
            self.mic = pick_mic(self.sd)
            name = self.sd.query_devices(self.mic, "input")["name"]
            if name != last:
                print(f"  (mic: {name})", flush=True)
                last = name
            self._listen_on(self.mic, on_speech, on_interrupt)  # returns when pausing
            self.closed.set()
            self.go.wait()
            self.go.clear()

    def pause(self):
        self.switching.set()
        self.closed.wait(timeout=5)
        self.closed.clear()

    def resume(self):
        self.switching.clear()
        self.go.set()

    def _listen_on(self, device, on_speech, on_interrupt):
        mic = queue.Queue()
        ref = np.zeros(0, np.float32)    # speaker audio not yet matched to mic audio
        clean = np.zeros(0, np.float32)  # echo-cancelled mic, waiting to fill a VAD frame
        before = collections.deque(maxlen=10)  # keep ~0.3 s from just before speech starts
        u = {}                           # the utterance under way: frames, words so far, its stream
        # Waiting on Google happens here, never on this thread: a mic loop that stalls falls
        # behind the speakers, the echo canceller loses track of its voice, and it hears itself.
        waiter = concurrent.futures.ThreadPoolExecutor(1)  # one at a time, so words arrive in order

        def begin(frames):
            u.update(speech=list(frames), said=[], quiet=0, ignoring=False, check=None, stream=self.open())
            # ponytail: one flag, not one per thing heard; two things said within a second of each other share it
            self.over = time.monotonic() - self.voiced < 1.0
            u["stream"].push(np.concatenate(frames))

        def deliver(text, audio):
            text = self.words(text, audio)
            if text:
                self.clip = audio
                on_speech(text)

        def end():
            """Done: the last words come back on the waiter while the mic carries on."""
            said, check, stream, audio = u["said"], u["check"], u["stream"], np.concatenate(u["speech"])
            u.clear()
            self.vad.reset_states()

            def last_words():
                pieces = [*said, check[1].result() if check else "", stream.finish()]
                deliver(" ".join(t for t in pieces if t), audio)
            waiter.submit(last_words)

        def ask(kind):
            """Ask Google for the words so far, without waiting here; a fresh stream takes what follows."""
            u["check"] = (kind, waiter.submit(u["stream"].finish))
            # asleep, nothing more goes to Google until we know it was for it
            u["stream"] = Local(lambda audio: "") if kind == "wake" else self.open()

        try:
            with self.sd.InputStream(device=device, samplerate=RATE, channels=1, dtype="float32",
                                     blocksize=TEN_MS,
                                     callback=lambda data, *_: mic.put(data[:, 0].copy())):
                behind_said = 0.0
                while not self.switching.is_set():
                    try:
                        chunk = mic.get(timeout=0.2)
                    except queue.Empty:  # a vanished device stops sending; the watcher will reopen
                        continue
                    if GAIN != 1.0:
                        chunk = np.clip(chunk * GAIN, -1, 1)
                    if mic.qsize() > 100 and not u:  # over a second behind: skip the stale audio
                        lag = mic.qsize() / 100
                        skip = mic.qsize() - 10
                        for _ in range(skip):
                            mic.get_nowait()
                        if time.monotonic() - behind_said > 30:  # say so, but not every time
                            print(f"  (ears fell {lag:.1f}s behind; caught up)", flush=True)
                            behind_said = time.monotonic()
                    # keep the speakers' audio level with the mic's: a speaker chunk older than the mic
                    # audio in hand is one the canceller would pair with the wrong moment
                    for _ in range(len(self.render) - mic.qsize() // 2 - 5):
                        self.render.popleft()
                    if len(ref) < TEN_MS:
                        ref = np.concatenate([ref, self.render.popleft() if self.render
                                              else np.zeros(TEN_MS, np.float32)])
                    clean = np.concatenate([clean, self._cancel_echo(chunk, ref[:TEN_MS])])
                    ref = ref[TEN_MS:]
                    if len(clean) < FRAME:
                        continue
                    frame, clean = clean[:FRAME], clean[FRAME:]

                    p = self.vad(frame)
                    self.level = float(np.sqrt(np.mean(frame ** 2))) * p
                    if u:
                        u["speech"].append(frame)
                        u["quiet"] = u["quiet"] + 1 if p < 0.35 else 0
                        if u["ignoring"]:  # asleep, and not for it: let it finish without sending it
                            if u["quiet"] > END or len(u["speech"]) > LONGEST:
                                u.clear()
                                self.vad.reset_states()
                            continue
                        u["stream"].push(frame, speech=p >= 0.35)
                        if u["check"] and u["check"][1].done():  # Google's answer to ask() is back
                            kind, piece = u["check"][0], u["check"][1].result()
                            u["check"] = None
                            if kind == "wake":
                                heard = " ".join(t for t in [*u["said"], piece] if t)
                                if wake(heard) is None:  # not "Hey Jugnu": show it, and send no more
                                    deliver(heard, np.concatenate(u["speech"]))
                                    u.update(ignoring=True)
                                else:
                                    begin(u["speech"])  # for it: hear it all again, in one piece
                                continue
                            u["said"].append(piece)
                            text = " ".join(t for t in u["said"] if t)
                            if u["quiet"] >= SOON and (not text or sounds_finished(text)):
                                u["stream"].close()  # it only got the pause
                                said, audio = text, np.concatenate(u["speech"])
                                u.clear()
                                self.vad.reset_states()
                                waiter.submit(deliver, said, audio)  # after any words still on their way
                                continue
                        if not u["check"]:
                            if len(u["speech"]) == WAKE_WINDOW and not self.awake():
                                ask("wake")
                            elif u["quiet"] == SOON:  # a pause: if it sounds finished, answer now
                                ask("soon")
                        if u["quiet"] > END or len(u["speech"]) > LONGEST:
                            end()
                        continue
                    if self.talking():  # be surer while it talks: leftover echo mustn't stop it
                        self.loud = self.loud + 1 if p > 0.8 and self.learned > LEARN else 0
                        if self.loud >= BARGE:
                            on_interrupt()
                            self.loud = 0
                            begin([*before, frame])
                            continue
                    elif p > HEAR:
                        begin([*before, frame])
                        continue
                    before.append(frame)
        finally:
            if u.get("stream"):
                u["stream"].close()
            waiter.shutdown(wait=True)  # words still on their way get handed over

    def _cancel_echo(self, mic, ref):
        to_frame = lambda x: self.rtc.AudioFrame(
            (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes(), RATE, 1, TEN_MS)
        self.aec.process_reverse_stream(to_frame(ref))
        frame = to_frame(mic)
        self.aec.process_stream(frame)  # cleans the frame in place
        return np.frombuffer(bytes(frame.data), np.int16).astype(np.float32) / 32767


def pick_mic(sd):
    """JUGNU_MIC in .env: parts of mic names in order of preference, e.g.
    "headset, microphone array". The first one plugged in wins; none, or no
    setting, means Windows' default mic."""
    devices = list(enumerate(sd.query_devices()))
    for want in filter(None, (w.strip().lower() for w in os.getenv("JUGNU_MIC", "").split(","))):
        for i, d in devices:
            if d["max_input_channels"] > 0 and want in d["name"].lower():
                return i
    return None


def reopen_audio(ear, player):
    """Close the mic and speakers, refresh the device list, reopen on what's there now."""
    import sounddevice as sd
    if ear:
        ear.pause()
    if player:
        player.close()
    # ponytail: private calls, but sounddevice's documented way to see new devices
    sd._terminate()
    sd._initialize()
    if player:
        player.open()
    if ear:
        ear.resume()


def follow_devices(ear, player, every=2.0):
    """Windows: when headphones or mics come or go, reopen audio on what's there now.
    Windows' own device counts stay current; the audio library's list doesn't."""
    import ctypes
    winmm = ctypes.windll.winmm
    count = lambda: (winmm.waveInGetNumDevs(), winmm.waveOutGetNumDevs())
    seen = count()
    while True:
        time.sleep(every)
        if count() == seen:
            continue
        time.sleep(1.5)  # let Windows finish switching its default devices
        seen = count()
        print("  (audio devices changed)", flush=True)
        reopen_audio(ear, player)


def mic_check():
    """python ears.py mics : list the mics, then show how loud the chosen one hears you."""
    import sounddevice as sd
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
    seen = set()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["name"] not in seen:
            seen.add(d["name"])
            print(f"  {d['name']}")
    mic = pick_mic(sd)
    print(f"\nListening on: {sd.query_devices(mic, 'input')['name']}. Talk for 8 seconds...")
    with sd.InputStream(device=mic, samplerate=RATE, channels=1, dtype="float32") as stream:
        for _ in range(32):
            level = float(np.sqrt(np.mean(stream.read(RATE // 4)[0] ** 2)))
            print(f"  {'#' * min(60, int(level * 600)):60} {level:.3f}")
    print("Talking should reach well past 0.010; under 0.003 counts as silence.")


def load():
    """The Ears, or None if JUGNU_EARS=off or Google can't be reached (then it's type-only)."""
    if os.getenv("JUGNU_EARS", "on").lower() == "off":
        return None
    try:
        return Ears()
    except Exception as e:
        print(f"ears: off ({str(e).splitlines()[0][:300]})")
        return None


def google_check():
    """python ears.py google : streams a recorded sentence to Google, as if spoken, and times
    how soon after you stop the words are ready, for each endpointing setting."""
    import wave
    from dotenv import load_dotenv
    from scipy.signal import resample_poly
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
    t = time.perf_counter()
    google = Google([*NAME_HINTS, *pronounce.load()])
    print(f"connecting: {google.check() or 'ok'} in {time.perf_counter() - t:.1f}s")
    clip = os.getenv("JUGNU_TEST_CLIP") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "samples", "google-tts", "04-en-IN-Algieba.wav")
    if not os.path.exists(clip):
        print(f"no test clip at {clip}: record one spoken sentence as a WAV and set JUGNU_TEST_CLIP to it")
        return
    with wave.open(clip) as w:
        rate, audio = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), np.int16) / 32768
    audio = resample_poly(audio, RATE // 1000, rate // 1000).astype(np.float32)
    for ending in ("standard", "short", "supershort"):
        google.first = google.request(True, ending)
        stream = google.open()
        for i in range(0, len(audio), FRAME):  # in real time, as a mic would
            stream.push(audio[i:i + FRAME], speech=np.abs(audio[i:i + FRAME]).max() > 0.01)
            time.sleep(FRAME / RATE)
        stopped = time.perf_counter()
        for _ in range(SOON):  # then the pause Jugnu waits out, as when you stop talking
            stream.push(np.zeros(FRAME, np.float32), speech=False)
            time.sleep(FRAME / RATE)
        text = stream.finish()
        done = time.perf_counter() - stopped
        print(f"{ending}: words ready {done:.2f}s after you stopped ({SOON * FRAME / RATE:.1f}s of that is Jugnu waiting out your pause)"
              f"\n    {text or stream.error}")


if __name__ == "__main__" and sys.argv[1:] == ["mics"]:
    mic_check()
elif __name__ == "__main__" and sys.argv[1:] == ["google"]:
    google_check()
elif __name__ == "__main__":
    assert wake("Hey Jugnu, what does SQL do?") == "what does SQL do?"
    assert wake("Hey, Jugnu.") == ""
    assert wake("Jugnu what time is it") == "what time is it"
    assert wake("Okay Jug New, run it.") == "run it."
    assert wake("I read Jugnu's book") is None  # its name mid-sentence isn't a call
    assert wake("The weather is nice.") is None
    for said, want in [("Let's start fresh.", ["fresh"]), ("Okay, make a new conversation.", ["fresh"]),
                       ("Log everything.", ["log"]), ("Can you log everything we talked about today?", ["log"]),
                       ("Log everything and start a new conversation.", ["log", "fresh"]),
                       ("Okay. Save this conversation, then let's start over.", ["log", "fresh"]),
                       ("I had a new conversation with Sam yesterday.", []), ("Did you log everything?", []),
                       ("Don't start a new conversation.", []), ("Start fresh and tell me about SQL joins.", []),
                       ("What's a fresh start for the ESM-2 run?", [])]:
        assert conversation(said) == want, (said, conversation(said))
    assert wake("Ey Jugnu!") == ""
    assert wake("Hey, Jugnu. Hey, Jugnu.") == ""  # seen in use
    assert wake("Hey Jugnu, can you do that? Hey, Jugnu.") == "can you do that?"
    assert wake("Jugnu, read Jugnu's paper.") == "read Jugnu's paper."
    assert sleep("Okay, go to sleep.") and sleep("Stop listening, Jugnu.") and sleep("Bye!")
    assert sleep("That's all for now, thanks.") and sleep("Thank you, go home.")
    assert sleep("Got to sleep.") and sleep("And then go home.")  # both seen in use
    assert not sleep("How much sleep do I need?") and not sleep("Go home and rest your eyes.")
    assert sleep("Yeah, I think it's okay for now. Jugnu, go to sleep.")  # seen in use
    assert sleep("I wanted to go to sleep.") and sleep("I want you to go to sleep now.")
    assert sleep("Okay, I think you should go to sleep.")  # seen in use
    assert learning("Switch on the learning mode.") is True and learning("Let's practise") is True
    assert learning("Learning mode off, please.") is False and learning("What is the learning mode?") is None
    assert learning("I am learning to switch on the lights") is None and LEARN_STOP.fullmatch("Stop.")
    assert vision("Turn your vision on.") is True and vision("Can you look at my screen?") is True and vision("Switch on your eyes") is True
    assert vision("Turn your vision off, please.") is False and vision("Stop looking at my screen.") is False and vision("Vision off") is False
    assert browser_window("Show me the browser.") is True and browser_window("Turn off headless mode, please") is True
    assert browser_window("Hide the browser") is False and browser_window("Headless mode on.") is False
    assert browser_window("Switch on the head less mode") is False and browser_window("Run the browser in the background") is False
    assert browser_window("Show the browser history for LinkedIn") is None and browser_window("Open the browser and find flights") is None
    assert browser_window("Is the browser hidden?") is None and browser_window("Bring the browser to the front") is True
    assert vision("Is your vision on?") is None and vision("Don't look at my screen") is None and vision("I have poor vision") is None
    assert LOOK.match("Look at my screen and tell me what this error means") and LOOK.match("Hey Jugnu, can you look at the screen, what is this?")
    assert not LOOK.match("Don't look at my screen") and not LOOK.match("Look at my screen recording from yesterday")
    assert LEARN_BACK.fullmatch("Go back. Go back.") and LEARN_SKIP.fullmatch("SQL skip") and LEARN_SKIP.fullmatch("Skip it.")  # seen in use
    assert not LEARN_BACK.fullmatch("Switch back to Claude.") and not LEARN_SKIP.fullmatch("Switch to Sonnet.")
    assert APPLY.fullmatch("Okay, apply the change.") and UNDO.fullmatch("Undo the last change, please.")
    assert not APPLY.fullmatch("Apply the change to the figure legend.") and not UNDO.fullmatch("Undo it in the document.")
    assert LEARN_OWN.search("A word of my own.") and not LEARN_OWN.search("The basics.")
    assert stop("Cancel this job.") == "" and stop("I think you should cancel this job.") == ""  # seen in use
    assert stop("Please stop.") == "" and stop("You should stop codons from being read through.") is None
    assert lesson("Switch to Sonnet", "Switch to Sonet.") == [("sonet", "Sonnet")] and lesson("Hey Jugnu", "Hey, Jugnu.") == []
    assert lesson("Hey Jugnu", "hey jug no") == [("jug no", "Jugnu")] and lesson("Switch to DeepSeek", "switch to deep sea") == [("deep sea", "DeepSeek")]
    assert lesson("Hey Jugnu", "What did you say?") is None and lesson("Go to sleep", "Okay, go to sleep.") == []
    before = dict(HEARD); HEARD.clear(); HEARD.update({"sonet": "Sonnet", "jug no": "Jugnu", "rock": "Grok"})
    assert heard_as("Switch to Sonet now") == "Switch to Sonnet now" and heard_as("switch to rock") == "switch to Grok"
    assert heard_as("play some rock, jug no", names=True) == "play some rock, Jugnu"  # only its name, outside commands
    assert wake("Hey jug no, play some rock") == "play some rock" and heard_as("sonets and rocks") == "sonets and rocks"
    HEARD.clear(); HEARD.update(before)
    assert stop("Stop.") == "" and stop("Jugnu, stop!") == "" and stop("Never mind.") == ""
    assert stop("Cancel that, read the other folder instead.") == "read the other folder instead."
    assert stop("Stop codon in exon 3, check it.") is None and stop("Don't stop.") is None
    assert stop("Stop, stop, stop. What I want you to do is find the latest paper.") is not None
    assert private("Go private.") is True and private("Jugnu, turn on private mode, please.") is True
    assert private("Switch to private mode.") is True and private("Private mode on.") is True
    assert private("Private mode off.") is False and private("Go back to Google.") is False
    assert private("Okay, turn off private mode.") is False and private("Exit private mode now.") is False
    assert private("What does private mode do?") is None and private("Is private mode on?") is None
    assert private("Go private with the data before the paper is out.") is None
    assert private("Can you turn on private mode?") is True  # seen in use
    assert private("Okay, so private mode is working. Now I want you to switch back to online mode.") is False
    assert private("Switch off the private mode.") is False and private("Go online.") is False
    assert private("Switch to normal mode, please.") is False and private("Go back to regular mode.") is False
    assert private("Go online and find the paper.") is None and private("Is it in online mode?") is None
    assert private("Tell me about normal mode.") is None and private("I don't want to use private mode.") is None
    w = Whisper.__new__(Whisper); w.terms = ["Wi-Fi", "SQL", "CARD", "PubMed", "Jugnu"]
    assert w.unprimed("Wi-Fy, SQL, CARD, PubMed, Jugnoo Are you still in the private mode?") == "Are you still in the private mode?"
    assert w.unprimed("SQL is a query language.") == "SQL is a query language."  # one term: you said it
    assert not sleep("Go to sleep mode first. Then read the paper.")
    now = time.monotonic()
    own = [(now - 3, "Let me see."), (now - 2, "Thanks, Sam. I'll be here whenever you're ready.")]
    assert echo("Let me see. Thanks Sam.", own) and echo("Let me see.", own) and echo("Right.", [(now, "Right.")])
    assert echo("Sam Hey.", [(now, "Hey, Sam. What can I do for you?")])
    def checked(*errors):  # Google's check at start, with these results in turn: are the word hints kept?
        g, errors, asked = Google.__new__(Google), list(errors), []
        g.request = lambda hints: asked.append(hints)
        g.open = lambda: types.SimpleNamespace(push=lambda a: None, finish=lambda timeout: None, error=errors.pop(0))
        return g.check(), asked
    assert checked(None) == ("", [True]) and checked(TimeoutError(), None) == ("", [True, True])  # slow isn't a no
    assert checked(ValueError("bad phrase"), None) == (" (without word hints)", [True, False])
    assert checked(TimeoutError(), TimeoutError(), None) == (" (without word hints)", [True, True, False])
    spoke = Ears.__new__(Ears)
    spoke.render, spoke.learned, spoke.voiced = collections.deque(), 0.0, 0.0
    spoke.played(np.ones(320, np.float32))
    assert time.monotonic() - spoke.voiced < 0.5 and spoke.learned > 0
    assert whose_voice("Learn my voice.") == "learn" == whose_voice("Can you recognise only my voice?")
    assert whose_voice("Only listen to me.") is True and whose_voice("Listen to my voice only") is True and whose_voice("voice lock on") is True
    assert whose_voice("Listen to everyone.") is False and whose_voice("Turn off the voice lock") is False
    assert whose_voice("Forget my voice.") == "forget" and whose_voice("Why are you recognizing someone else's voice?") is None
    assert whose_voice("Don't listen to everyone") is None and whose_voice("I lost my voice") is None
    assert whose_voice("That wasn't me.") == "stricter" == whose_voice("That was not my voice") == whose_voice("That was someone else.")
    assert whose_voice("That wasn't me who sent the email, check again") is None
    assert SLEEP.fullmatch("Just go to sleep mode.") and SLEEP.fullmatch("Go to sleep mode") and not SLEEP.fullmatch("What is sleep mode?")
    assert echo("right Hey Jugnu.", [(now, "Right."), (now, "I'm doing well, thanks for asking.")])  # seen in use
    assert not echo("Hey Jugnu, read me the paper.", [(now, "Right.")]) and not echo("Hey Jugnu.", [(now, "Right.")])
    assert not echo("Yes.", own) and not echo("Read me the page on SQL.", own)
    assert not echo("Yes.", [(now, "Shall I send it? Say yes or no.")])  # answering it isn't an echo
    assert not echo("Let me see.", [(now - 60, "Let me see.")])  # long ago: you said it
    assert looping("Wi-Fi, SQL. " * 20) and not looping("What does SQL do when the Wi-Fi drops?")
    assert looping("Wi-Fi, SQL. Wi-Fi, SQL. Wi-Fi, SQL. Wi-")  # the short ones seen in real use
    assert not looping("Stop, stop, stop, stop. What I want you to do is find the latest paper.")
    assert wake("Hi, Jugnoo. What are you doing?") == "What are you doing?"
    assert phantom("Bye.", 0.6) and phantom("Thank you.", 1.0) and phantom("Thanks for watching!", 1.2)
    assert not phantom("Bye.", 2.0) and not phantom("Bye, Jugnu.", 1.0) and not phantom("Yes, go ahead.", 1.0)
    assert not phantom("Yeah.", 0.5) and not phantom("Okay.", 0.5) and not phantom("Yes.", 0.4)
    assert sounds_finished("Which model am I talking to right now?") and sounds_finished("Hey Jugnu.")
    assert sounds_finished("I'm working on giving you a face.") and sounds_finished("Yes.")
    assert not sounds_finished("So I was thinking about the") and not sounds_finished("I want you to read it and")
    assert not sounds_finished("What I wanted to do is, um.") and not sounds_finished("Let me think...")
    assert not sounds_finished("First the paper, then the") and not sounds_finished("")
    assert not sounds_finished("Look at all the folders and then take help of the council, and.")

    # The listening loop, with a pretend mic, detector and Google: what goes to Google, and what comes back
    class FakeStream:
        def __init__(self, text): self.text, self.pushed, self.error = text, 0, None
        def push(self, audio, speech=True): self.pushed += len(audio)
        def finish(self): return self.text
        close = lambda self: None
    last_frame = [0.0]
    def run(script, texts, awake=True, kind=None):
        """script: (seconds, talking?) pieces; texts: what each stream Google opens will say."""
        e, got, streams, done = Ears.__new__(Ears), [], [], threading.Event()
        e.render, e.learned, e.loud, e.level, e.terms, e.voiced = collections.deque(), 0.0, 0, 0.0, [], 0.0
        e.talking, e.awake, e.switching = (lambda: False), (lambda: awake), threading.Event()
        e._cancel_echo = lambda mic, ref: mic
        fed = [0]
        class Vad:
            def __call__(self, frame):
                fed[0] -= 1; last_frame[0] = time.perf_counter()
                return 0.9 if np.abs(frame).max() > 0.05 else 0.0
            reset_states = lambda self: None
        e.vad = Vad()
        e.open = lambda: streams.append((kind or FakeStream)(texts.pop(0))) or streams[-1]
        audio = np.concatenate([(0.2 if loud else 0.0) * np.sin(np.arange(int(s * RATE)) / 3)
                                for s, loud in script]).astype(np.float32)
        class Mic:
            def __init__(self, callback, **_): self.callback = callback
            def __enter__(self):
                def feed():
                    for i in range(0, len(audio), TEN_MS):
                        while fed[0] > 20:  # don't run ahead of the loop (it would skip "stale" audio)
                            time.sleep(0.0005)
                        self.callback(audio[i:i + TEN_MS, None])
                        fed[0] += TEN_MS / FRAME
                    while fed[0] >= 1:  # a part-frame left over never reaches the detector
                        time.sleep(0.001)
                    e.switching.set()
                threading.Thread(target=feed, daemon=True).start()
            def __exit__(self, *a): pass
        e.sd = type("SD", (), {"InputStream": lambda self, **k: Mic(**k)})()
        e._listen_on(None, got.append, lambda: None)
        return got, streams
    got, streams = run([(0.5, 0), (2, 1), (1.2, 0)], ["What does SQL do?", ""])
    assert got == ["What does SQL do?"], got  # answered after the pause
    got, streams = run([(0.5, 0), (2, 1), (1.2, 0), (1, 1), (2.3, 0)], ["So I was thinking about the", "Wi-Fi password.", ""])
    assert got == ["So I was thinking about the Wi-Fi password."], got  # paused mid-thought: kept listening
    got, streams = run([(0.5, 0), (5, 1), (2.3, 0)], ["We should get dinner"], awake=False)
    assert got == ["We should get dinner"] and len(streams) == 1 and streams[0].pushed <= WAKE_WINDOW * FRAME, streams[0].pushed
    got, streams = run([(0.5, 0), (5, 1), (2.3, 0)], ["Hey Jugnu, what's the", "Hey Jugnu, what's the weather?", ""], awake=False)
    assert got == ["Hey Jugnu, what's the weather?"] and streams[1].pushed > 5 * RATE, got  # for it: all of it, once
    got, streams = run([(0.5, 0), (0.6, 1), (1.2, 0)], ["", ""])
    assert got == [], got  # a cough: nothing
    got, streams = run([(0.5, 0), (2, 1), (2.5, 0)], ["Tell me about", "", ""])
    assert got == ["Tell me about"], got  # trailed off: answered after the long pause anyway
    slow = []  # Google slow to answer: the mic must keep going meanwhile
    class SlowStream(FakeStream):
        def finish(self): time.sleep(1.0); slow.append(time.perf_counter()); return self.text
    got, streams = run([(0.5, 0), (2, 1), (3, 0)], ["What does SQL do?", ""], kind=SlowStream)
    assert got == ["What does SQL do?"] and last_frame[0] < slow[0], (got, last_frame, slow)  # never waited on Google
    st = Stream.__new__(Stream)  # Google already done up to your last word: no waiting for it to close
    st.google, st.waiting, st.chunks, st.done = type("G", (), {})(), np.zeros(0, np.float32), queue.Queue(), threading.Event()
    st.finals, st.sent, st.spoke, st.heard_upto, st.error = [], 0, 0, 0, None
    st.push(np.ones(RATE), speech=True); st.push(np.zeros(RATE), speech=False)
    st.finals, st.heard_upto = ["Done."], RATE - 800
    t0 = time.perf_counter(); assert st.finish() == "Done." and time.perf_counter() - t0 < 0.1
    e = Ears.__new__(Ears)
    e.private, e.whisper, e.google = True, (lambda audio: f"whisper heard {len(audio)}"), "not used"
    s = e.open(); s.push(np.zeros(800)); s.push(np.zeros(800))
    assert s.finish() == "whisper heard 1600"  # private: the audio stays here
    e.private, e.google = False, None
    assert e.open().finish() == ""  # Google never came up, and not private: deaf, not crashed
    print("ok")
