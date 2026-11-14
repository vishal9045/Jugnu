"""Argon mode: Gemini 4 Argon as Jugnu's brain, with read-only hands.

"Switch to Argon" hands the conversation to Gemini; "switch back to Claude" (or
to Haiku/Sonnet/Opus, or "go back to automatic") hands it back. Each side is told
what was said while the other was in charge.

Argon can search Google, search PubMed, list and read your files (text, PDF,
Word), and consult GPT and DeepSeek. It can't write or change files, run
commands or drive your browser: for those it tells you to switch back to Claude.

Like Claude, it streams: each sentence is spoken as soon as it's written.
It runs on your Gemini API key, billed per use (Claude runs on your plan), so
each turn's token count is printed.
"""
import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import council
import pubmed

MODEL = os.getenv("JUGNU_ARGON_MODEL", "gemini-4-argon")
STEPS = 8          # tool rounds per answer before it must reply
KEEP = 60          # history entries kept (older ones dropped at a turn boundary)
MAX_CHARS = 60000  # of any one file handed to it
SENTENCE_END = re.compile(r"[.!?:;][\"')\]]*\s+|\n\s*\n")  # where a streamed sentence is complete

NO_SHARE = (f"Never send anything from {council.NO_SHARE_WHAT} to the advisers; follow that silently. "
            if council.NO_SHARE_WHAT else "")

# The brain Argon stands in for (JUGNU_BRAIN), what the user says to get back to it, and which of
# Argon's two advisers sit on the council as it's set up
USUAL = {"claude": "Claude", "openai": "GPT", "gemini": "Gemini", "deepseek": "DeepSeek", "xai": "Grok"}.get(
    council._brain, "their usual model")
BACK = "switch back to Claude" if council._brain == "claude" else "go back to automatic"
ADVISERS = [a for a, key in (("gpt", None), ("deepseek", "DEEPSEEK_API_KEY"))
            if a in council.MEMBERS and (not key or os.getenv(key))]
_ASK = (f", and {'two advisers' if len(ADVISERS) > 1 else 'an adviser'} ({', '.join('ask_' + a for a in ADVISERS)})"
        if ADVISERS else "")

RULES = f"""Right now Jugnu's brain is Gemini ({MODEL}), switched in at the user's request;
normally it is {USUAL}. You have Google Search (use it for anything current; never
read URLs aloud), PubMed (search_pubmed, read_abstract: use these, not Google, for
the literature), the user's files read-only (list_folder, read_file: text, PDF and Word){_ASK}.
You cannot write or change files, run commands or use their browser: for those, say
{USUAL} can do it if they say "{BACK}". "Go to sleep" and "{BACK}" are the user's own voice
commands, handled before their words reach you: you can't sleep or switch models yourself, so never
say you have, and don't guess why one didn't work; tell them the exact words to say.
Before a search, or reading something long, say a short line first. {NO_SHARE}No markdown, lists or headings: everything you write is spoken."""

FUNCTIONS = [
    {"name": "search_pubmed", "description": "Search PubMed, newest first. For any question about the "
     "literature. Returns PMID, date, journal, first author, title.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"},
                                                     "count": {"type": "integer"}}, "required": ["query"]}},
    {"name": "read_abstract", "description": "The abstract of one PubMed paper, by PMID.",
     "parameters": {"type": "object", "properties": {"pmid": {"type": "string"}}, "required": ["pmid"]}},
    {"name": "list_folder", "description": "What's in a folder on the user's computer (e.g. their Downloads).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "read_file", "description": "Read a file on the user's computer: text, code, CSV, PDF or Word.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "ask_gpt", "description": "A second opinion from GPT. Slow." + council.NO_SHARE,
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "ask_deepseek", "description": "A second opinion from DeepSeek, strong on reasoning. Slow. "
     "" + council.NO_SHARE,
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
]
FUNCTIONS = [f for f in FUNCTIONS if not f["name"].startswith("ask_") or f["name"][4:] in ADVISERS]


def list_folder(path):
    p = Path(os.path.expanduser(path))
    if not p.is_dir():
        return f"No folder at {p}."
    items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = [f"{x.name}{'/' if x.is_dir() else ''}" for x in items[:300]]
    return f"{p} ({len(items)} items):\n" + "\n".join(lines)


def read_file(path):
    p = Path(os.path.expanduser(path))
    if p.is_dir():
        return list_folder(path)
    if not p.is_file():
        return f"No file at {p}."
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "Can't read PDFs yet: pypdf isn't installed (uv pip install --python .venv pypdf)."
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
    elif suffix == ".docx":  # a Word file is a zip of XML: pull the paragraphs' text out
        xml = zipfile.ZipFile(p).read("word/document.xml").decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
    else:
        text = p.read_text(encoding="utf-8", errors="replace")
    cut = len(text) > MAX_CHARS
    return text[:MAX_CHARS] + (f"\n[... cut: the file is {len(text)} characters]" if cut else "")


def _stream(contents, system):
    """Gemini's answer as it's written: one dict per server-sent event."""
    key = os.environ["GEMINI_API_KEY"]
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
            "tools": [{"google_search": {}}, {"functionDeclarations": FUNCTIONS}],
            "tool_config": {"include_server_side_tool_invocations": True}}  # Google Search alongside ours
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:streamGenerateContent?alt=sse",
        json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("data:"):
                    yield json.loads(line[5:])
    except urllib.error.HTTPError as e:  # the provider's reason, without the key
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace').replace(key, '***')[:300]}") from None


def merge(parts, piece):
    """Add one streamed part to the reply: text deltas join the text before them;
    tool calls and thought signatures are kept whole, as Gemini needs them back."""
    last = parts[-1] if parts else None
    if ("text" in piece and last is not None and "text" in last and "functionCall" not in last
            and bool(piece.get("thought")) == bool(last.get("thought"))):
        last["text"] += piece["text"]
        if "thoughtSignature" in piece:
            last["thoughtSignature"] = piece["thoughtSignature"]
    else:
        parts.append(dict(piece))


class Argon:
    def __init__(self, system, allowed=lambda tool, args: None, log=lambda record: None):
        """allowed(tool, args): None if fine, else why not (e.g. a password file)."""
        self.system = system + "\n\n" + RULES
        self.allowed, self.log = allowed, log
        self.history, self.turns = [], []  # turns: (you, argon) said since the user switched to Argon

    def clear(self):
        self.history.clear()
        self.turns.clear()

    def seed(self, turns):
        """What was said before the switch, so Argon picks up the thread."""
        for you, said in turns:
            self.history += [{"role": "user", "parts": [{"text": you}]},
                             {"role": "model", "parts": [{"text": said}]}]

    def _trim(self):  # keep it bounded; cut only where one of the user's messages starts
        while len(self.history) > KEEP:
            i = next((k for k in range(1, len(self.history)) if self.history[k]["role"] == "user"
                      and "text" in self.history[k]["parts"][0]), len(self.history))
            self.history = self.history[i:]

    def _run(self, call):
        name, args = call["name"], call.get("args", {})
        why = self.allowed(name, args)
        if why:
            return f"Not allowed: it {why}."
        try:
            if name == "search_pubmed":
                return pubmed.search(args["query"], min(int(args.get("count") or 10), 30))
            if name == "read_abstract":
                return pubmed.abstract(args["pmid"])
            if name == "list_folder":
                return list_folder(args["path"])
            if name == "read_file":
                return read_file(args["path"])
            if name == "ask_gpt":
                with council.consulting("GPT"):
                    return council._gpt(args["question"])
            if name == "ask_deepseek":
                with council.consulting("DeepSeek"):
                    return council._deepseek(args["question"])
            return f"No tool called {name}."
        except Exception as e:  # tell Argon, don't crash the turn
            return f"{name} failed: {e}"

    async def ask(self, text, meanwhile=lambda said: None):
        """Like jugnu.ask(): speaks as it goes through meanwhile; returns ("", seconds)."""
        start = time.perf_counter()
        self._trim()
        before = len(self.history)
        self.history.append({"role": "user", "parts": [{"text": text}]})
        try:
            return await self._answer(text, meanwhile, start)
        except Exception:
            del self.history[before:]  # a broken turn leaves no half-told story behind
            raise

    async def _answer(self, text, meanwhile, start):
        said, used, tokens, first = [], [], 0, None

        def speak(line):
            nonlocal first
            first = first or round(time.perf_counter() - start, 2)
            said.append(line)
            meanwhile(line)
        loop = asyncio.get_running_loop()
        for _ in range(STEPS):
            chunks = asyncio.Queue()  # the stream is read in a thread; each piece comes back here

            def pump():
                try:
                    for chunk in _stream(self.history, self.system):
                        loop.call_soon_threadsafe(chunks.put_nowait, chunk)
                except Exception as e:
                    loop.call_soon_threadsafe(chunks.put_nowait, e)
                finally:
                    loop.call_soon_threadsafe(chunks.put_nowait, None)
            reader = asyncio.create_task(asyncio.to_thread(pump))
            parts, pending, step_tokens = [], "", 0
            while (chunk := await chunks.get()) is not None:
                if isinstance(chunk, Exception):
                    raise chunk
                step_tokens = chunk.get("usageMetadata", {}).get("totalTokenCount") or step_tokens  # last one is the step's total
                for piece in (chunk.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
                    merge(parts, piece)
                    if piece.get("text") and not piece.get("thought"):
                        pending += piece["text"]
                        cut = max((m.end() for m in SENTENCE_END.finditer(pending)), default=0)
                        if cut:  # a sentence is complete: say it now
                            speak(pending[:cut].strip())
                            pending = pending[cut:]
            await reader
            if pending.strip():
                speak(pending.strip())
            tokens += step_tokens
            self.history.append({"role": "model", "parts": parts})  # whole: Gemini needs its signatures back
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                break
            replies = []
            for call in calls:
                what = call.get("args", {})
                print(f"  (argon {call['name']}: {what.get('query') or what.get('path') or what.get('pmid') or ''})",
                      flush=True)
                used.append(call["name"])
                result = await asyncio.to_thread(self._run, call)
                replies.append({"functionResponse": {"name": call["name"], "id": call.get("id"),
                                                     "response": {"result": result}}})
            self.history.append({"role": "user", "parts": replies})
        seconds = round(time.perf_counter() - start, 2)
        answer = " ".join(said)
        self.turns.append((text, answer))
        print(f"  ({MODEL}: {tokens} tokens)", flush=True)
        self.log({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "you": text, "jugnu": answer,
                  "model": [MODEL], "consulted": used, "seconds": seconds, "first_words": first,
                  "tokens": tokens, "error": False})
        return "", seconds


if __name__ == "__main__":
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "notes.txt").write_text("The meeting moved to Friday.", encoding="utf-8")
    with zipfile.ZipFile(d / "a.docx", "w") as z:
        z.writestr("word/document.xml", "<w:p><w:r><w:t>First para</w:t></w:r></w:p><w:p><w:t>Second</w:t></w:p>")
    assert read_file(str(d / "notes.txt")) == "The meeting moved to Friday."
    assert read_file(str(d / "a.docx")).split() == ["First", "para", "Second"]
    assert "notes.txt" in list_folder(str(d)) and "No file" in read_file(str(d / "nope.txt"))

    # one turn with a tool round, against a fake Gemini
    chunk = lambda *parts, used=None: {"candidates": [{"content": {"role": "model", "parts": list(parts)}}],
                                       **({"usageMetadata": {"totalTokenCount": used}} if used else {})}
    steps = iter([
        [chunk({"text": "Let me "}), chunk({"text": "look."}, used=4),
         chunk({"functionCall": {"name": "read_file", "args": {"path": str(d / "notes.txt")}, "id": "1"},
                "thoughtSignature": "sig"}, used=10)],
        [chunk({"text": "It says the meeting "}), chunk({"text": "moved to Friday."}), chunk({"text": "", "thoughtSignature": "s2"}, used=12)]])
    globals()["_stream"] = lambda contents, system: iter(next(steps))
    spoken, logged = [], []
    a = Argon("You are Jugnu.", log=logged.append)
    a.seed([("hi", "Hello there.")])
    out = asyncio.run(a.ask("what's in my notes?", spoken.append))
    assert out[0] == "" and spoken == ["Let me look.", "It says the meeting moved to Friday."], spoken
    fr = a.history[-2]["parts"][0]["functionResponse"]
    assert fr["name"] == "read_file" and fr["response"]["result"] == "The meeting moved to Friday."
    assert a.history[-3]["parts"][0] == {"text": "Let me look."}  # deltas joined back into one part
    assert a.history[-3]["parts"][1]["thoughtSignature"] == "sig"  # handed back to Gemini intact
    assert a.history[-1]["parts"] == [{"text": "It says the meeting moved to Friday.", "thoughtSignature": "s2"}]
    assert logged[0]["tokens"] == 22 and a.turns[-1][1].endswith("Friday.")
    assert logged[0]["first_words"] is not None
    n = len(a.history)  # a stream that breaks: the turn is undone, not left half-written

    def broken(contents, system):
        yield chunk({"text": "Half a sen"})
        raise RuntimeError("HTTP 503: overloaded")
    globals()["_stream"] = broken
    try:
        asyncio.run(a.ask("and another?", spoken.append))
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    assert len(a.history) == n, (len(a.history), n)
    blocked = Argon("x", allowed=lambda tool, args: "touches a password or key file")
    assert "Not allowed" in blocked._run({"name": "read_file", "args": {"path": ".env"}})
    print("ok")
