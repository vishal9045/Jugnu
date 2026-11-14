"""Jugnu's brain when it isn't Claude: GPT, Gemini, DeepSeek, Grok, OpenRouter, a local model
through Ollama, or any server that speaks OpenAI's chat API, on your API key.

One loop serves all of them, so every brain gets the same hands: your files (read, write, edit; pictures
are shown to it), commands, web search and web pages, PubMed, the council, and the browser and Google
Drive/Gmail when those are set up. The hands belong to Jugnu, not to the model, so the same risk() check in jugnu.py asks
you out loud before anything risky, whichever brain is thinking.

Settings (.env):
  JUGNU_BRAIN        which provider (a key of PROVIDERS); "claude" means jugnu.py's own path
  JUGNU_BRAIN_MODEL  the model for work (reading, searching, files, code)
  JUGNU_BRAIN_FAST   the model for conversation (default: the same)
  JUGNU_BRAIN_DEEP   the model for "think hard" (default: the same)
  JUGNU_BRAIN_URL    custom only: the server's base URL (ending in /v1, usually)
The key is the provider's usual one (OPENAI_API_KEY, GEMINI_API_KEY, ...), shared with the council.

Like Claude, it streams: each sentence is spoken as soon as it's written.
"""
import asyncio
import base64
import glob as globlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import argon    # read_file, list_folder, and merge() for Gemini's streamed parts
import council
import pubmed

ROOT = Path(__file__).resolve().parent
STEPS = 40          # tool rounds per answer before it must reply
KEEP = 120          # history entries kept (older ones dropped where one of the user's messages starts)
MAX_OUT = 30000     # characters of any one tool result handed back (files get argon.MAX_CHARS)
COMMAND_SECONDS = 300
SENTENCE_END = argon.SENTENCE_END
WINDOWS = sys.platform == "win32"

# dialect: how the provider is spoken to. "openai" is the chat-completions API most providers copy;
# Gemini is spoken to natively, for Google Search and the thought signatures its tool calls need.
PROVIDERS = {
    "openai":     {"name": "GPT", "dialect": "openai", "base": "https://api.openai.com/v1", "key": "OPENAI_API_KEY"},
    "gemini":     {"name": "Gemini", "dialect": "gemini", "key": "GEMINI_API_KEY",
                   "fast": os.getenv("JUGNU_GEMINI_FAST", "gemini-4-hydrogen"), "work": argon.MODEL},
    "deepseek":   {"name": "DeepSeek", "dialect": "openai", "base": "https://api.deepseek.com", "key": "DEEPSEEK_API_KEY",
                   "fast": "deepseek-flash", "work": council.DEEPSEEK_MODEL,
                   "sees": "deepseek-flash"},  # the one of its models that takes pictures: a turn with the screen goes to it  # deepseek-chat was retired in July 2026
    "xai":        {"name": "Grok", "dialect": "openai", "base": "https://api.x.ai/v1", "key": "XAI_API_KEY"},
    "openrouter": {"name": "OpenRouter", "dialect": "openai", "base": "https://openrouter.ai/api/v1",
                   "key": "OPENROUTER_API_KEY"},
    "ollama":     {"name": "your local model", "dialect": "openai", "base": "http://localhost:11434/v1", "key": None},
    "custom":     {"name": "your model", "dialect": "openai", "base": None, "key": "JUGNU_BRAIN_KEY"},
}


def settings():
    """This brain's settings from .env, or ValueError saying what's missing (in words for a person)."""
    provider = os.getenv("JUGNU_BRAIN", "claude").strip().lower()
    if os.getenv("JUGNU_BRAIN_AUTH", "api").strip().lower() == "subscription":
        if provider != "openai":
            raise ValueError(f"JUGNU_BRAIN_AUTH=subscription works for GPT (Codex) only so far, not {provider}.")
        codex_command()  # Codex must be there
        # one model (Codex's, or JUGNU_BRAIN_MODEL), thinking harder for work and "think hard"
        return {"provider": "openai", "dialect": "codex", "name": "GPT on your ChatGPT plan",
                "model": os.getenv("JUGNU_BRAIN_MODEL", "").strip() or None,
                "fast": "gpt-fast", "work": "gpt-work", "deep": "gpt-deep"}
    if provider not in PROVIDERS:
        raise ValueError(f"JUGNU_BRAIN is {provider!r}; it should be claude or one of {', '.join(PROVIDERS)}.")
    p = dict(PROVIDERS[provider], provider=provider)
    if provider == "custom":
        p["base"] = os.getenv("JUGNU_BRAIN_URL", "").strip().rstrip("/")
        if not p["base"]:
            raise ValueError("JUGNU_BRAIN is custom but JUGNU_BRAIN_URL (the server's address) is empty.")
    elif provider == "ollama":
        p["base"] = os.getenv("JUGNU_BRAIN_URL", "").strip().rstrip("/") or p["base"]
    if p["key"] and not os.getenv(p["key"]) and provider != "custom":
        raise ValueError(f"The brain is {p['name']}, but there's no {p['key']} in the settings.")
    p["work"] = os.getenv("JUGNU_BRAIN_MODEL", "").strip() or p.get("work")
    if not p["work"]:
        raise ValueError(f"The brain is {p['name']}, but JUGNU_BRAIN_MODEL (which model) is empty.")
    p["fast"] = os.getenv("JUGNU_BRAIN_FAST", "").strip() or p.get("fast") or p["work"]
    p["deep"] = os.getenv("JUGNU_BRAIN_DEEP", "").strip() or p["work"]
    return p


def guest(seat, model=None):
    """A council member's settings as a stand-in brain ("switch to Grok", "switch to Carbon"): on the model
    named, else the one it advises with, on the key it advises on. ValueError says, in words to speak, why not."""
    if seat == "gpt" and not (model and os.getenv("OPENAI_API_KEY")):  # the council's GPT is Codex on your ChatGPT plan
        try:
            codex_command()
        except ValueError:
            raise ValueError("GPT can't take over: Codex isn't installed.") from None
        return {"provider": "openai", "dialect": "codex", "name": "GPT", "seat": seat, "model": None,
                "fast": "gpt-fast", "work": "gpt-work", "deep": "gpt-deep"}
    provider = "openai" if seat == "gpt" else seat
    p = dict(PROVIDERS[provider], provider=provider, seat=seat)
    name = p["name"][0].upper() + p["name"][1:]
    if p["key"] and not os.getenv(p["key"]):
        raise ValueError(f"{name} can't take over: there's no {p['key']} in the settings.")
    if model:  # asked for by name: that one for everything
        p["fast"] = p["work"] = p["deep"] = model
        return p
    model = (os.getenv(council.CHAT_SEATS[seat][3], "").strip() if seat in council.CHAT_SEATS  # its main model: the one
             else {"gemini": council.GEMINI_MODEL, "deepseek": council.DEEPSEEK_MODEL}.get(seat) or p.get("work"))  # it advises with
    if not model:
        raise ValueError(f"{name} can't take over: no model is named for it in the settings.")
    p["work"] = p["deep"] = model
    if seat == "xai" and not p.get("fast"):  # Grok's quick model changes name with each release: ask its key which it has
        quick = sorted(m for m in models(seat) if "non-reasoning" in m or "fast" in m)
        if quick:
            p["fast"], p["sees"] = quick[-1], model  # the quick one may take no pictures: a turn with the screen goes to the main one
    p["fast"] = p.get("fast") or model
    return p


_MODELS = {}  # seat -> the model ids its key can use, asked once a session


def models(seat):
    """What this seat's key can use, from the provider itself; [] if it has no key or won't say (asked again next time)."""
    if seat not in _MODELS:
        try:
            if seat == "gemini":
                if not os.getenv("GEMINI_API_KEY"):
                    return []
                _MODELS[seat] = council._gemini_models()
            else:
                p = PROVIDERS["openai" if seat == "gpt" else seat]
                key = os.getenv(p["key"] or "", "")
                if (p["key"] and not key) or not p["base"]:
                    return []
                req = urllib.request.Request(p["base"] + "/models", headers={"Authorization": f"Bearer {key}"} if key else {})
                with urllib.request.urlopen(req, timeout=15) as r:
                    _MODELS[seat] = [m["id"] for m in json.load(r).get("data", [])]
        except Exception:  # noqa: BLE001
            return []
    return _MODELS[seat]


def find_model(phrase, seats):
    """[(seat, model id)]: for each seat, its model whose name has every word said ("carbon" -> gemini-4-carbon,
    "grok 4.7" -> grok-4.7); the shortest such name, so "flash" isn't "flash-lite-preview"."""
    bits = lambda t: re.findall(r"[a-z]+|\d+", t.lower())
    words = [w for w in bits(phrase) if w not in ("a", "an", "the", "new", "model", "brain")]
    out = []
    for seat in seats if words else []:
        hits = [m for m in models(seat) if all(w in bits(m) for w in words)]
        if hits:
            out.append((seat, min(hits, key=len)))
    return out


def _post(url, body, headers, timeout=180, stream=False):
    """The response (open, for streaming) or its JSON; HTTP errors as the provider's reason, keys hidden."""
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        for v in headers.values():
            msg = msg.replace(v.split()[-1], "***")
        raise RuntimeError(f"HTTP {e.code}: {msg[:300]}") from None
    if stream:
        return r
    with r:
        return json.load(r)


def _sse(response):
    """Server-sent events: each data line's JSON, until [DONE]."""
    with response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            yield json.loads(data)


class OpenAIChat:
    """The chat-completions dialect: OpenAI, DeepSeek, Grok, OpenRouter, Ollama, llama.cpp, vLLM..."""

    def __init__(self, cfg):
        self.cfg, self.history = cfg, []
        key = os.getenv(cfg["key"] or "", "")
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        if cfg["provider"] == "openrouter":
            self.headers["X-Title"] = "Jugnu"

    def user(self, text, picture=None):
        self.history.append({"role": "user", "content": text if not picture else [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + picture}}]})

    def unsee(self, start, note=""):
        """Pictures shown from here on are taken back out: seen once, they aren't sent, and paid for, with every later turn."""
        for m in self.history[start:]:
            if m["role"] == "user" and isinstance(m["content"], list):
                m["content"] = m["content"][0]["text"] + note

    def seed(self, turns):
        for you, said in turns:
            self.history += [{"role": "user", "content": you}, {"role": "assistant", "content": said}]

    def is_turn_start(self, m):
        return m["role"] == "user"

    def stream(self, model, system, tools):
        """Yields ("text", piece) as it's written, then ("end", message, calls, tokens)."""
        body = {"model": model, "messages": [{"role": "system", "content": system}] + self.history,
                "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        text, reasoning, calls, tokens = "", "", {}, 0
        for chunk in _sse(_post(self.cfg["base"] + "/chat/completions", body, self.headers, stream=True)):
            if chunk.get("usage"):
                tokens = chunk["usage"].get("total_tokens") or tokens
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                    yield "text", delta["content"]
                reasoning += delta.get("reasoning_content") or ""
                for tc in delta.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    c["id"] = tc.get("id") or c["id"]
                    fn = tc.get("function") or {}
                    c["name"] += fn.get("name") or ""
                    c["arguments"] += fn.get("arguments") or ""
        message = {"role": "assistant", "content": text or None}
        if reasoning and self.cfg["provider"] == "deepseek":  # its thinking, handed back within the turn
            message["reasoning_content"] = reasoning
        found = []
        if calls:
            message["tool_calls"] = []
            for i, c in sorted(calls.items()):
                c["id"] = c["id"] or f"call_{i}"
                message["tool_calls"].append({"id": c["id"], "type": "function",
                                              "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}})
                try:
                    args = json.loads(c["arguments"] or "{}")
                except ValueError:
                    args = {"_unreadable": c["arguments"]}
                found.append((c["id"], c["name"], args if isinstance(args, dict) else {}))
        yield "end", message, found, tokens

    def add(self, message):
        self.history.append(message)

    def results(self, calls, outputs):
        for (cid, name, _), out in zip(calls, outputs):
            self.history.append({"role": "tool", "tool_call_id": cid, "content": out})

    def quick(self, model, system, prompt, timeout=60):
        data = _post(self.cfg["base"] + "/chat/completions",
                     {"model": model, "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": prompt}]}, self.headers, timeout)
        return (data["choices"][0]["message"].get("content") or "").strip()


class GeminiNative:
    """Gemini spoken to natively: Google Search alongside our tools, and its thought signatures kept."""

    URL = "https://generativelanguage.googleapis.com/v1beta/models/"

    def __init__(self, cfg):
        self.cfg, self.history = cfg, []
        self.headers = {"x-goog-api-key": os.environ[cfg["key"]]}

    def user(self, text, picture=None):
        self.history.append({"role": "user", "parts": [{"text": text}] + (
            [{"inlineData": {"mimeType": "image/jpeg", "data": picture}}] if picture else [])})

    def unsee(self, start, note=""):
        for m in self.history[start:]:
            if any("inlineData" in p for p in m["parts"]):
                m["parts"] = [{"text": m["parts"][0]["text"] + note}]

    def seed(self, turns):
        for you, said in turns:
            self.history += [{"role": "user", "parts": [{"text": you}]},
                             {"role": "model", "parts": [{"text": said}]}]

    def is_turn_start(self, m):
        return m["role"] == "user" and "text" in m["parts"][0]

    def stream(self, model, system, tools):
        decls = [{"name": t["name"], "description": t["description"],
                  **({"parametersJsonSchema": t["parameters"]} if t.get("mcp") else {"parameters": t["parameters"]})}
                 for t in tools]
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": self.history,
                "tools": [{"google_search": {}}] + ([{"functionDeclarations": decls}] if decls else []),
                "tool_config": {"include_server_side_tool_invocations": True}}
        parts, tokens = [], 0
        url = f"{self.URL}{model}:streamGenerateContent?alt=sse"
        for chunk in _sse(_post(url, body, self.headers, stream=True)):
            tokens = chunk.get("usageMetadata", {}).get("totalTokenCount") or tokens
            for piece in (chunk.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
                argon.merge(parts, piece)
                if piece.get("text") and not piece.get("thought"):
                    yield "text", piece["text"]
        calls = [(p["functionCall"].get("id"), p["functionCall"]["name"], p["functionCall"].get("args") or {})
                 for p in parts if "functionCall" in p]
        yield "end", {"role": "model", "parts": parts}, calls, tokens

    def add(self, message):
        self.history.append(message)

    def results(self, calls, outputs):
        self.history.append({"role": "user", "parts": [
            {"functionResponse": {"name": name, "id": cid, "response": {"result": out}}}
            for (cid, name, _), out in zip(calls, outputs)]})

    def quick(self, model, system, prompt, timeout=60):
        data = _post(f"{self.URL}{model}:generateContent",
                     {"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": prompt}]}]}, self.headers, timeout)
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


# ---- the hands ----

def _obj(required=(), **props):
    return {"type": "object", "properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()},
            "required": list(required)}


SHELL = "PowerShell" if WINDOWS else "bash"
TOOLS = [
    {"name": "read_file", "description": "Read a file on the user's computer: text, code, CSV, PDF or Word; "
     "a picture (png, jpg, webp, gif) is shown to you.",
     "parameters": _obj(["path"], path=("string", "Full path of the file."))},
    {"name": "list_folder", "description": "What's in a folder on the user's computer (e.g. their Downloads).",
     "parameters": _obj(["path"], path=("string", "Full path of the folder."))},
    {"name": "find_files", "description": "Find files by name pattern, e.g. '**/*.pdf' or '*report*'. Up to 300 paths.",
     "parameters": _obj(["pattern"], pattern=("string", "Glob pattern; ** goes into subfolders."),
                        folder=("string", "Folder to search in (default: the user's home folder)."))},
    {"name": "search_in_files", "description": "Search inside text files for a regular expression; "
     "returns matching lines with file and line number (up to 200).",
     "parameters": _obj(["pattern"], pattern=("string", "Regular expression (case-insensitive)."),
                        folder=("string", "Folder to search (default: the user's home folder)."),
                        files=("string", "Which files, as a glob (default '**/*').") )},
    {"name": "write_file", "description": "Create a file with this content (parent folders are made). "
     "Overwriting an existing file needs the user's OK, which you'll be told about.",
     "parameters": _obj(["path", "content"], path=("string", "Full path."), content=("string", "The whole content."))},
    {"name": "edit_file", "description": "Replace exact text in a file. old_text must appear exactly once unless "
     "replace_all is true; include surrounding lines to make it unique.",
     "parameters": {"type": "object", "required": ["path", "old_text", "new_text"], "properties": {
         "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
         "replace_all": {"type": "boolean"}}}},
    {"name": "run_command", "description": f"Run a {SHELL} command on the user's computer and get its output "
     f"(stopped after {COMMAND_SECONDS // 60} minutes). Risky commands (deleting, moving, killing programs, "
     "system changes) wait for the user's spoken OK.",
     "parameters": _obj(["command", "description"], command=("string", f"The {SHELL} command."),
                        description=("string", "What it does, in a few plain words (the user may hear this)."),
                        folder=("string", "Folder to run it in (default: Jugnu's own)."))},
    {"name": "web_search", "description": "Search the web. Returns each result's title, address and a line of "
     "its text; read one in full with fetch_page. Never read URLs aloud.",
     "parameters": _obj(["query"], query=("string", "What to search for."), count=("integer", "How many (default 8)."))},
    {"name": "fetch_page", "description": "Read a web page as text. Never read URLs aloud.",
     "parameters": _obj(["url"], url=("string", "The page's address."))},
    {"name": "search_pubmed", "description": "Search PubMed, newest first. For any question about the literature. "
     "Returns PMID, date, journal, first author, title.",
     "parameters": _obj(["query"], query=("string", "A PubMed query."), count=("integer", "How many (default 10)."))},
    {"name": "read_abstract", "description": "The abstract of one PubMed paper, by PMID.",
     "parameters": _obj(["pmid"], pmid=("string", "The PMID."))},
]
# Read-only tools that run without asking (like Claude's WebSearch, PubMed and council)
FREE = {"web_search", "fetch_page", "search_pubmed", "read_abstract", "gemini_models", "use_model", "ask_claude", "ask_gpt",
        "ask_gemini", "ask_deepseek", "ask_xai", "ask_openrouter", "ask_ollama", "ask_custom"}
READ_ONLY = {"read_file", "list_folder", "find_files", "search_in_files"} | (FREE - {"use_model"})

MEMBERS = {  # council members: tool name -> (name, ask one question, key it needs)
    "ask_claude": ("Claude", lambda q: council._claude(q), None),
    "ask_gpt": ("GPT", lambda q: council._gpt(q), None),
    "ask_gemini": ("Gemini", lambda q: council._gemini(q), "GEMINI_API_KEY"),
    "ask_deepseek": ("DeepSeek", lambda q: council._deepseek(q), "DEEPSEEK_API_KEY"),
    **{f"ask_{s}": (n, lambda q, s=s: council._chat(s, q), k or m) for s, (n, k, _, m) in council.CHAT_SEATS.items()},
}
HANDS_ON = {"ask_claude", "ask_gpt", "ask_gemini", "ask_deepseek"} | {f"ask_{s}" for s in council.HANDS_ON}  # can work on a folder


SHOW = ("string", "Optional: full paths of documents for it to read with the question, one per line (reports, PDFs, "
        "Word, text). It opens and reads them itself; nothing is run. Use this to show it a report or a run's results.")


def council_tools(also=()):
    """The council as tools; also: seats to add (a stand-in may consult the usual brain, which isn't on its own council)."""
    out = []
    for name, (who, _, needs) in MEMBERS.items():
        seat = name.removeprefix("ask_")
        ready = name in HANDS_ON or os.getenv(council.CHAT_SEATS[seat][3])  # a chat seat needs its model named
        if (seat in council.MEMBERS or seat in also) and ready and (not needs or os.getenv(needs)):
            if name in HANDS_ON:
                out.append({"name": name, "description": f"A second opinion from {who} (slow). Give files to have "
                            "it read documents (nothing is run); give folder only when code is to be run or audited, "
                            "hands-on on a copy in its own sandbox." + council.NO_SHARE,
                            "parameters": _obj(["question"], question=("string", "The question or the task."), files=SHOW,
                                               folder=("string", "Optional: a folder of code to run or audit hands-on."))})
            else:
                out.append({"name": name, "description": f"A second opinion from {who} (slow). Answers questions "
                            "and reads documents given as files; it can't work on a folder." + council.NO_SHARE,
                            "parameters": _obj(["question"], question=("string", "The question."), files=SHOW)})
    if os.getenv("GEMINI_API_KEY"):
        out.append({"name": "gemini_models", "description": council.gemini_models.description, "parameters": _obj()})
    if {"gemini", "deepseek"} & (set(council.MEMBERS) | set(also)):
        out.append({"name": "use_model", "description": council.use_model.description,
                    "parameters": _obj(["member", "model"], member=("string", "gemini or deepseek"),
                                       model=("string", "The exact model id."))})
    return out


def as_claude(name, args):
    """This tool call as Claude's tool of the same kind, for risk() and describe() in jugnu.py."""
    path = args.get("path") or args.get("folder") or ""
    return {
        "read_file": ("Read", {"file_path": path}),
        "list_folder": ("Glob", {"pattern": "*", "path": path}),
        "find_files": ("Glob", {"pattern": args.get("pattern", ""), "path": path}),
        "search_in_files": ("Grep", {"pattern": args.get("pattern", ""), "path": path}),
        "write_file": ("Write", {"file_path": path, "content": args.get("content", "")}),
        "edit_file": ("Edit", {"file_path": path, "old_string": args.get("old_text", ""),
                               "new_string": args.get("new_text", "")}),
        "run_command": ("Bash", {"command": args.get("command", ""), "description": args.get("description", "")}),
        "fetch_page": ("WebFetch", {"url": args.get("url", "")}),
        "web_search": ("WebSearch", {"query": args.get("query", "")}),
    }.get(name) or (f"mcp__{name}" if "__" in name  # an MCP tool: server__tool
                    else f"mcp__pubmed__{name}" if name in ("search_pubmed", "read_abstract")
                    else f"mcp__council__{name}" if name in MEMBERS or name in ("gemini_models", "use_model")
                    else name, args)


def _cap(text, limit=MAX_OUT):
    return text if len(text) <= limit else text[:limit] + f"\n[... cut: {len(text)} characters in all]"


SKIP = re.compile(r"[\\/](\.git|node_modules|\.venv|venv|__pycache__)([\\/]|$)")


def find_files(pattern, folder=""):
    base = Path(os.path.expanduser(folder or "~"))
    hits = [h for h in globlib.iglob(str(base / pattern), recursive=True) if not SKIP.search(h)]
    out = sorted(hits[:5000])[:300]
    return "\n".join(out) + (f"\n[{len(hits)} matches; first 300 shown]" if len(hits) > 300 else "") \
        if out else f"Nothing matches {pattern} in {base}."


def search_in_files(pattern, folder="", files="**/*"):
    rx, base, hits = re.compile(pattern, re.I), Path(os.path.expanduser(folder or "~")), []
    for f in globlib.iglob(str(base / (files or "**/*")), recursive=True):
        if SKIP.search(f) or not os.path.isfile(f) or os.path.getsize(f) > 2_000_000:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{f}:{n}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n[stopped at 200 matches]"
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: skip
    return "\n".join(hits) or f"No line matches {pattern!r} in {base}."


def write_file(path, content):
    p = Path(os.path.expanduser(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {p}."


def edit_file(path, old_text, new_text, replace_all=False):
    p = Path(os.path.expanduser(path))
    text = p.read_text(encoding="utf-8")
    n = text.count(old_text)
    if not old_text or n == 0:
        return f"Not changed: that text isn't in {p}."
    if n > 1 and not replace_all:
        return f"Not changed: that text is in {p} {n} times. Include more around it, or set replace_all."
    p.write_text(text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1),
                 encoding="utf-8")
    return f"Edited {p} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''})."


def run_command(command, folder=""):
    shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command] if WINDOWS \
        else ["bash", "-lc", command]
    try:
        r = subprocess.run(shell, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=COMMAND_SECONDS, cwd=os.path.expanduser(folder) if folder else ROOT)
    except subprocess.TimeoutExpired:
        return f"Stopped after {COMMAND_SECONDS} seconds without finishing."
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    if len(out) > MAX_OUT:  # the end of a long output is usually what matters
        out = f"[... first {len(out) - MAX_OUT} characters cut]\n" + out[-MAX_OUT:]
    return f"exit code {r.returncode}\n{out.strip()}"


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Jugnu voice assistant)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(3_000_000)
        kind = r.headers.get("Content-Type", "")
        charset = r.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    if "html" in kind or text.lstrip().startswith("<"):
        text = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    return _cap(text.strip())


SEARCH = "https://html.duckduckgo.com/html/?"  # DuckDuckGo's plain page: no key, no account


def results(page, count=8):
    """The results on a DuckDuckGo plain page, numbered: title, address, a line of text. "" if it holds none."""
    tidy = lambda s: " ".join(html.unescape(re.sub(r"<[^>]+>", "", s)).split())
    lines = [tidy(s) for s in re.findall(r'<a\b[^>]*class="result__snippet"[^>]*>(.*?)</a>', page, re.S)]
    out = []
    for tag, title in re.findall(r'(<a\b[^>]*class="result__a"[^>]*>)(.*?)</a>', page, re.S):
        href = html.unescape((re.search(r'href="([^"]*)"', tag) or [None, ""])[1])
        url = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]  # its links go by way of a redirect
        line = lines.pop(0) if lines else ""
        if url.startswith("http") and "duckduckgo.com/y.js" not in url:  # that's an advert
            out.append(f"{len(out) + 1}. {tidy(title)}\n   {url}" + (f"\n   {line}" if line else ""))
    return "\n".join(out[:count])


def web_search(query, count=8):
    # ponytail: one free search page, read as a person's browser would; it sometimes refuses programs, and then
    # the brain is told to search in the browser. A paid search API if this ever needs to be dependable.
    req = urllib.request.Request(SEARCH + urllib.parse.urlencode({"q": query}),
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jugnu"})
    with urllib.request.urlopen(req, timeout=20) as r:
        found = results(r.read(2_000_000).decode("utf-8", errors="replace"), count)
    if not found:
        raise RuntimeError("the search page gave no results (it sometimes refuses programs): search in the browser instead, if you have one")
    return found


IMAGES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def picture_of(path, size=1920):
    """An image file as a JPEG in base64, no bigger than size on its long side: how a model is shown it."""
    from PIL import Image
    with Image.open(os.path.expanduser(path)) as im:
        im = im.convert("RGB")
        im.thumbnail((size, size))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=80)
    return base64.b64encode(out.getvalue()).decode()


class McpServer:
    """One of Jugnu's MCP servers (the browser, Google Drive/Gmail), kept running for this brain.
    The `mcp` library holds a server's pipes inside one task, so that task lives as long as the server."""

    def __init__(self, name, spec):
        self.name, self.spec, self.tools, self.session, self.error = name, spec, [], None, None

    async def start(self, timeout=90):
        self._ready, self._stop = asyncio.Event(), asyncio.Event()
        self.task = asyncio.create_task(self._serve())
        await asyncio.wait_for(self._ready.wait(), timeout)
        if self.error:
            raise self.error

    async def _serve(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            if "url" in self.spec:  # one already running: Jugnu's own browser, which every brain shares
                import mcp.client.streamable_http as http
                pipes = (getattr(http, "streamable_http_client", None) or http.streamablehttp_client)(self.spec["url"])  # mcp 2.x / 1.x
            else:
                pipes = stdio_client(StdioServerParameters(command=self.spec["command"], args=self.spec.get("args", []),
                                                           env={**os.environ, **self.spec.get("env", {})}))
            async with pipes as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.tools = (await session.list_tools()).tools
                    self.session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as e:  # noqa: BLE001 - reported by start()
            self.error = e
        finally:
            self.session = None
            self._ready.set()

    async def call(self, tool, args):
        if not self.session:
            return f"The {self.name} connection is closed."
        res = await self.session.call_tool(tool, args)
        out = [c.text if getattr(c, "type", "") == "text" else f"[{getattr(c, 'type', 'item')}]"
               for c in res.content]
        failed = getattr(res, "is_error", None) or getattr(res, "isError", None)  # mcp 2.x / 1.x
        return ("Error: " if failed else "") + _cap("\n".join(out))

    async def close(self):
        self._stop.set()
        try:
            await asyncio.wait_for(self.task, 10)
        except Exception:  # noqa: BLE001 - shutting down regardless
            pass


def rules(cfg, read_only=False):
    hands = ("You can read the user's files (read_file, list_folder, find_files, search_in_files) but not change "
             "them or run commands; if they want that, say so plainly." if read_only else
             "You can read, write and edit the user's files and run {shell} commands. Risky actions (deleting, "
             "moving, overwriting, stopping programs, system changes, touching password or key files) pause while "
             "the user is asked out loud; if they say no, don't try another way round.").replace("{shell}", SHELL)
    web = ("You have Google Search for anything current." if cfg["dialect"] == "gemini" else
           "You can search the web (web_search) and read any page (fetch_page).")
    models_note = (" To know which Gemini models exist, use gemini_models, never memory; it and use_model are about "
                   "the council's Gemini adviser, not about you." if os.getenv("GEMINI_API_KEY") else "")
    heavier = [m for m in dict.fromkeys([cfg["work"], cfg["deep"]]) if m != cfg["fast"]]
    on = cfg["fast"] + (f" for everyday answers, {' or '.join(heavier)} for heavier ones" if heavier else "")
    return (f"You are thinking on {cfg['name']}: {on}. The model giving each answer is named at the very end of "
            "these instructions: asked which model you are, say that one, never a tool's answer or a guess. "
            f"{hands} {web}{models_note} For the literature use "
            "search_pubmed and read_abstract, never the web. Before a search, or reading something long, say a "
            "short line first. No markdown, lists or headings: everything you write is spoken.")


BLIND = "\n\n[Vision is on, but this model can't be shown pictures: say so if they ask about their screen.]"


class Agent:
    """The same shape as argon.Argon: ask() speaks as it goes through meanwhile and returns ("", seconds)."""
    blind = False  # True once its model has refused a picture
    alone = False  # True for a council member reading documents: no council of its own to ask

    def __init__(self, system, gate=None, log=lambda record: None, servers=None, read_only=False,
                 holding=("One moment, let me look.", "Let me ask the others."), cfg=None):
        """gate(tool, args): awaited before any tool that isn't free; returns None to go ahead, or why not.
        Its tool and args are Claude's equivalents (see as_claude), so jugnu.permit() works unchanged."""
        self.cfg = cfg or settings()
        self.chat = (GeminiNative if self.cfg["dialect"] == "gemini" else OpenAIChat)(self.cfg)
        self.base_system, self.read_only = system, read_only
        self.system = system + "\n\n" + rules(self.cfg, read_only)
        self.gate, self.log, self.holding = gate, log, holding
        self.specs = servers or {}  # name -> how to reach it, as jugnu.browser_server()/google_server() give
        self.servers, self.mcp_names = {}, {}
        self.turns = []  # (you, it) since the last time Claude was told (for switching back and forth)
        self.shown = []  # pictures it opened with read_file, waiting to be shown to it

    async def connect(self):
        """Start the MCP servers; one that won't start is left out (and said why), not fatal."""
        for name, spec in self.specs.items():
            server = McpServer(name, spec)
            try:
                await server.start()
                self.servers[name] = server
            except Exception as e:  # noqa: BLE001
                print(f"  ({name} tools not available to {self.cfg['name']}: {str(e)[:150]})", flush=True)

    async def close(self):
        for s in self.servers.values():
            await s.close()

    def seed(self, turns):
        self.chat.seed(turns)

    def clear(self):
        self.chat.history.clear()
        self.turns.clear()

    def tools(self):
        own = [t for t in TOOLS if (not self.read_only or t["name"] in READ_ONLY)
               and not (t["name"] == "web_search" and self.cfg["dialect"] == "gemini")]  # Gemini has Google Search
        standin = self.cfg.get("seat")  # a stand-in consults the usual brain too, and never itself
        own += [t for t in ([] if self.alone else council_tools((council.SELF,) if standin else ()))
                if t["name"] != f"ask_{standin}" and (not self.read_only or t["name"] in READ_ONLY)]
        self.mcp_names.clear()
        if not self.read_only and not getattr(self, "light", False):
            for server in self.servers.values():
                for t in server.tools:
                    name = f"{server.name}__{t.name}"[:64]
                    self.mcp_names[name] = (server, t.name)
                    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)  # mcp 2.x / 1.x
                    own.append({"name": name, "description": (t.description or "")[:1000],
                                "parameters": schema or {"type": "object", "properties": {}}, "mcp": True})
        return own

    def _trim(self):
        h = self.chat.history
        while len(h) > KEEP:
            i = next((k for k in range(1, len(h)) if self.chat.is_turn_start(h[k])), len(h))
            del h[:i]

    async def _call(self, name, args):
        if name not in FREE and self.gate:
            tool, inp = as_claude(name, args)
            why = await self.gate(tool, inp)
            if why:
                return f"Not done: {why}"
        try:
            if name in self.mcp_names:
                server, tool = self.mcp_names[name]
                return await server.call(tool, args)
            if name in MEMBERS:
                who, fn, needs = MEMBERS[name]
                res = await council._consult(who, lambda q, fn=fn: fn(q), args, needs=needs)
                return res["content"][0]["text"]
            if self.read_only and name not in READ_ONLY:
                return f"{name} isn't available in this mode."
            if name == "read_file" and str(args.get("path", "")).lower().endswith(IMAGES):  # a picture: shown, not read out
                if self.blind:
                    return "That's a picture, and this model can't be shown pictures."
                self.shown.append(await asyncio.to_thread(picture_of, args["path"]))
                return "That's a picture: you are being shown it now."
            if name in ("gemini_models", "use_model"):
                res = await (council.gemini_models if name == "gemini_models" else council.use_model).handler(args)
                return res["content"][0]["text"]
            run = {
                "read_file": lambda: argon.read_file(args["path"]),
                "list_folder": lambda: argon.list_folder(args["path"]),
                "find_files": lambda: find_files(args["pattern"], args.get("folder", "")),
                "search_in_files": lambda: search_in_files(args["pattern"], args.get("folder", ""),
                                                           args.get("files") or "**/*"),
                "write_file": lambda: write_file(args["path"], args["content"]),
                "edit_file": lambda: edit_file(args["path"], args["old_text"], args["new_text"],
                                               bool(args.get("replace_all"))),
                "run_command": lambda: run_command(args["command"], args.get("folder", "")),
                "fetch_page": lambda: fetch_page(args["url"]),
                "web_search": lambda: web_search(args["query"], min(int(args.get("count") or 8), 20)),
                "search_pubmed": lambda: pubmed.search(args["query"], min(int(args.get("count") or 10), 30)),
                "read_abstract": lambda: pubmed.abstract(args["pmid"]),
            }.get(name)
            if run is None:
                return f"No tool called {name}."
            return _cap(await asyncio.to_thread(run))
        except KeyError as e:
            return f"{name} needs {e.args[0]}."
        except Exception as e:  # noqa: BLE001 - told to the model, not fatal to the turn
            return f"{name} failed: {str(e)[:500]}"

    async def ask(self, text, meanwhile=lambda said: None, model=None, already=lambda: False,
                  on_tool=lambda name, args: None, light=False, picture=None):
        """light: just talk, so the browser's and Drive's tools (some 25,000 tokens of description, sent and
        paid for on every turn) are left out of this one. picture: the screen (a JPEG in base64), for this turn only."""
        start = time.perf_counter()
        self.light = light
        self._trim()
        before = len(self.chat.history)
        picture = None if self.blind else picture
        model = picture and self.cfg.get("sees") or model
        self.chat.user(text, picture)
        try:
            try:
                out = await self._answer(text, meanwhile, model or self.cfg["fast"], already, on_tool, start)
            except RuntimeError as e:  # a model that takes no pictures refuses the request: answer without, and remember
                if not (picture and re.match(r"HTTP (400|415|422)\b", str(e))):
                    raise
                del self.chat.history[before:]
                text, picture = text + BLIND, None
                self.chat.user(text)
                out = await self._answer(text, meanwhile, model or self.cfg["fast"], already, on_tool, start)
                self.blind = True
            self.chat.unsee(before)  # the screen, and any picture it opened: seen once
            return out
        except BaseException:  # an error or the user's "stop": the turn is undone, not left half-written
            del self.chat.history[before:]
            raise

    async def _answer(self, text, meanwhile, model, already, on_tool, start):
        said, used, tokens, first, shown = [], [], 0, None, None  # shown: where in the history a picture it opened sits
        loop = asyncio.get_running_loop()
        tools = self.tools()

        def speak(line):
            nonlocal first
            first = first or round(time.perf_counter() - start, 2)
            said.append(line)
            meanwhile(line)
        for step in range(STEPS):
            events = asyncio.Queue()
            offer = tools if step < STEPS - 1 else []  # the last round must answer, not reach for more

            def pump():  # the stream is read in a thread; each event comes back here
                try:
                    for ev in self.chat.stream(model, self.system + f"\n\nThis answer is coming from {model}.", offer):
                        loop.call_soon_threadsafe(events.put_nowait, ev)
                except Exception as e:  # noqa: BLE001
                    loop.call_soon_threadsafe(events.put_nowait, e)
                finally:
                    loop.call_soon_threadsafe(events.put_nowait, None)
            reader = asyncio.ensure_future(asyncio.to_thread(pump))
            pending, end = "", None
            try:
                while (ev := await events.get()) is not None:
                    if isinstance(ev, Exception):
                        raise ev
                    if ev[0] == "text":
                        pending += ev[1]
                        cut = max((m.end() for m in SENTENCE_END.finditer(pending)), default=0)
                        if cut:
                            speak(pending[:cut].strip())
                            pending = pending[cut:]
                    else:
                        end = ev
            except RuntimeError as e:  # it was shown a picture it can't take: on without it, and it says so
                if shown is None or not re.match(r"HTTP (400|415|422)\b", str(e)):
                    raise
                self.chat.unsee(shown, " This model can't be shown pictures: tell the user so.")
                shown, self.blind = None, True
                continue
            finally:
                if not reader.done():
                    reader.cancel()
            if pending.strip():
                speak(pending.strip())
            _, message, calls, step_tokens = end
            tokens += step_tokens or 0
            self.chat.add(message)
            if not calls:
                break
            if not said and not already() and any(c[1] in FREE for c in calls):
                speak(self.holding[1] if any(c[1].startswith("ask_") for c in calls) else self.holding[0])
            outputs = []
            for cid, name, args in calls:
                what = args.get("query") or args.get("path") or args.get("url") or args.get("command") or ""
                print(f"  ({name}: {str(what)[:120]})" if what else f"  ({name})", flush=True)
                used.append(name)
                tool, inp = as_claude(name, args)
                on_tool(tool, inp)
                outputs.append(await self._call(name, args))
            self.chat.results(calls, outputs)
            if self.shown:  # a picture can't ride in a tool's result: it follows as a message of its own
                shown = len(self.chat.history)
                for picture in self.shown:
                    self.chat.user("[The picture you opened.]", picture)
                self.shown.clear()
                model = self.cfg.get("sees") or model  # on to the one of its models that takes pictures
        seconds = round(time.perf_counter() - start, 2)
        answer = " ".join(said)
        self.turns.append((text, answer))
        print(f"  ({model}: {tokens} tokens)", flush=True)
        self.log({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "you": text, "jugnu": answer, "model": [model],
                  "consulted": used, "seconds": seconds, "first_words": first, "tokens": tokens, "error": False})
        return "", seconds

    async def quick(self, extra, prompt):
        """One answer with no tools, on the fast model (the side voice)."""
        return await asyncio.to_thread(self.chat.quick, self.cfg["fast"], self.base_system + extra, prompt)


# ---- a subscription brain: Codex on your ChatGPT plan ----

def codex_command():
    """How to start Codex's app server: JUGNU_CODEX (a command line), else codex on PATH, else
    Codex inside WSL (where the council's GPT already runs on Windows)."""
    custom = os.getenv("JUGNU_CODEX", "").strip()
    if custom:
        return custom.split() if not WINDOWS else ["cmd", "/c", custom]
    found = shutil.which("codex")
    if found:
        return [found, "app-server"]
    if WINDOWS and shutil.which("wsl.exe"):
        return ["wsl.exe", "-e", "bash", "-lc", "codex app-server"]
    raise ValueError("The brain is GPT on your ChatGPT plan, but Codex isn't installed "
                     "(npm install -g @openai/codex, then codex login).")


def wsl_path(path):
    """C:\\Users\\you -> /mnt/c/Users/you: how Codex inside WSL sees a Windows folder."""
    p = str(path).replace("\\", "/")
    return f"/mnt/{p[0].lower()}{p[2:]}" if re.match(r"^[A-Za-z]:", p) else p


EFFORT = {"fast": "low", "work": "medium", "deep": "high"}  # one model, thinking harder as the job asks


class CodexAgent:
    """GPT through Codex's app server (JSON-RPC over its stdin/stdout), signed in with your ChatGPT plan.
    It stays running, so a turn costs no startup; Codex brings its own hands (shell, file edits, web search),
    and asks before commands it doesn't trust: Jugnu answers those with the same risk() check, out loud
    when it's risky. What Codex doesn't bring (the council, PubMed, the browser, Drive and Gmail) Jugnu hands it
    as tools of its own, run here by an Agent's hands with the same checks: Codex calls back for each one.
    Same shape as Agent: ask(), quick(), seed(), clear(), turns."""

    def __init__(self, system, gate=None, log=lambda record: None, servers=None,
                 holding=("One moment, let me look.", "Let me ask the others."), cfg=None, command=None):
        self.cfg = cfg or {"name": "GPT on your ChatGPT plan", "model": os.getenv("JUGNU_BRAIN_MODEL", "").strip() or None}
        self.base_system, self.gate, self.log, self.holding = system, gate, log, holding
        self.system = system + "\n\n" + CODEX_RULES
        self.command = command or codex_command()
        self.turns, self.context, self.thread, self.side_thread = [], "", None, None
        self.fresh = False
        self.calls, self.next_id, self.listeners = {}, 0, {}  # thread id -> where its events go
        self.cwd = wsl_path(Path.home()) if self.command[0].lower().startswith("wsl") else str(Path.home())
        self.hands = Agent("", gate=gate, servers=servers, cfg={  # an Agent for its hands alone: it never thinks
            "name": self.cfg.get("name", "GPT"), "seat": self.cfg.get("seat"), "dialect": "openai", "provider": "codex",
            "key": None, "fast": "", "work": "", "deep": ""})
        self.own = []  # Jugnu's tools as Codex is told of them

    # -- the pipe --
    async def connect(self):
        await self.hands.connect()
        self.own = [{"type": "function", "name": t["name"], "description": t["description"], "inputSchema": t["parameters"]}
                    for t in self.hands.tools() if t["name"] not in CODEX_HAS]
        self.proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read())
        await self.request("initialize", {"clientInfo": {"name": "jugnu", "title": "Jugnu", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})  # tools of ours are that, so far
        await self._send({"method": "initialized", "params": {}})
        self.thread = await self._new_thread(self.system)

    async def close(self):
        if getattr(self, "proc", None) and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        await self.hands.close()

    async def _send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method, params):
        self.next_id += 1
        rid, done = self.next_id, asyncio.get_running_loop().create_future()
        self.calls[rid] = done
        await self._send({"id": rid, "method": method, "params": params})
        return await done

    async def _read(self):
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # not protocol: ignore
                if "method" not in msg:  # an answer to one of ours
                    done = self.calls.pop(msg.get("id"), None)
                    if done and not done.done():
                        if "error" in msg:
                            done.set_exception(RuntimeError(f"Codex: {msg['error'].get('message', msg['error'])}"))
                        else:
                            done.set_result(msg.get("result") or {})
                elif "id" in msg:  # Codex asking us something (an approval)
                    asyncio.create_task(self._answer(msg))
                else:  # an event: to its thread's turn (the main one and the side voice can overlap)
                    tid = (msg.get("params") or {}).get("threadId")
                    for put in ([self.listeners[tid]] if tid in self.listeners else list(self.listeners.values())):
                        put(msg)
        finally:  # Codex has gone: fail whatever was waiting on it
            for done in self.calls.values():
                if not done.done():
                    done.set_exception(RuntimeError("Codex stopped running."))
            for put in list(self.listeners.values()):
                put({"method": "codex/exited", "params": {}})

    async def _answer(self, msg):
        method, p = msg["method"], msg.get("params") or {}
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            command = p.get("command") or " ".join(p.get("command_argv") or [])
            why = await self.gate("Bash", {"command": command, "description": p.get("reason") or ""}) \
                if self.gate else None
            result = {"decision": "decline" if why else "accept"}
        elif method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            why = await self.gate("Write", {"file_path": p.get("grantRoot") or "files outside your folders",
                                            "content": ""}) if self.gate else None
            result = {"decision": "decline" if why else "accept"}
        elif method == "item/tool/call":  # one of Jugnu's own tools: run here, by the same hands and checks as any brain's
            args = p.get("arguments")
            args = json.loads(args) if isinstance(args, str) and args.strip().startswith("{") else args
            out = await self.hands._call(p.get("tool", ""), args if isinstance(args, dict) else {})
            result = {"contentItems": [{"type": "inputText", "text": out}], "success": True}
        else:  # questions for a screen it doesn't have (sign-ins, forms): say no rather than hang
            await self._send({"id": msg["id"], "error": {"code": -32601, "message": f"Jugnu can't answer {method}"}})
            return
        await self._send({"id": msg["id"], "result": result})

    async def _new_thread(self, instructions, read_only=False):
        params = {"cwd": self.cwd, "developerInstructions": instructions, "approvalPolicy": "untrusted",
                  "sandbox": "read-only" if read_only else "danger-full-access", "ephemeral": read_only,
                  **({"model": self.cfg["model"]} if self.cfg.get("model") else {})}
        try:
            r = await self.request("thread/start", {**params, **({"dynamicTools": self.own} if self.own and not read_only else {})})
        except RuntimeError as e:  # a Codex that won't take tools of ours: it still works, with its own hands only
            if not self.own or read_only:
                raise
            print(f"  (this Codex wouldn't take Jugnu's tools, so GPT has no council, PubMed, browser or Drive here: {str(e)[:120]})",
                  flush=True)
            self.own = []
            r = await self.request("thread/start", params)
        self.model = r.get("model") or self.cfg.get("model")  # Codex says which model the thread got: told to it each turn
        return r["thread"]["id"]

    # -- Jugnu's side --
    def seed(self, turns):
        if turns:
            self.context = "[Earlier today:\n" + "\n".join(f"User: {y}\nYou: {s}" for y, s in turns) + "]\n\n"

    def clear(self):
        self.turns.clear()
        self.context, self.fresh = "", True

    async def _turn(self, thread, text, effort, meanwhile, already=lambda: False, on_tool=lambda t, i: None, picture=None):
        """One turn on a thread; speaks sentences through meanwhile as they're written. Returns
        (all that was said, what it did)."""
        events, said, used, pending = asyncio.Queue(), [], [], [""]
        self.listeners[thread] = events.put_nowait

        def speak(line):
            if line:
                said.append(line)
                meanwhile(line)
        see = [{"type": "image", "url": "data:image/jpeg;base64," + picture}] if picture else []  # the screen, with this turn
        r = await self.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": text}] + see,
                                              "effort": effort})
        turn_id = (r.get("turn") or {}).get("id")
        try:
            while True:
                msg = await events.get()
                method, p = msg["method"], msg.get("params") or {}
                if p.get("threadId") not in (None, thread):
                    continue
                of = p.get("turnId") or (p.get("turn") or {}).get("id")
                if of and turn_id and of != turn_id:  # e.g. the end of a turn the user stopped: not this one
                    continue
                if method == "item/agentMessage/delta":
                    pending[0] += p.get("delta", "")
                    cut = max((m.end() for m in SENTENCE_END.finditer(pending[0])), default=0)
                    if cut:
                        speak(pending[0][:cut].strip())
                        pending[0] = pending[0][cut:]
                elif method == "item/completed" and (p.get("item") or {}).get("type") == "agentMessage":
                    speak(pending[0].strip())
                    pending[0] = ""
                elif method == "item/started":
                    item = p.get("item") or {}
                    kind = item.get("type", "")
                    if kind == "dynamicToolCall":  # one of Jugnu's own (the call itself arrives in _answer)
                        name = item.get("tool", "")
                        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
                        used.append(name)
                        what = args.get("query") or args.get("question") or args.get("url") or ""
                        print(f"  ({name}: {str(what)[:120]})" if what else f"  ({name})", flush=True)
                        on_tool(*as_claude(name, args))
                        if name in FREE and not said and not already():
                            speak(self.holding[1] if name.startswith("ask_") else self.holding[0])
                    elif kind in ("commandExecution", "fileChange", "webSearch", "mcpToolCall"):
                        used.append(kind)
                        what = item.get("command") or item.get("query") or ""
                        print(f"  ({kind}: {str(what)[:120]})" if what else f"  ({kind})", flush=True)
                        tool = {"commandExecution": "Bash", "fileChange": "Edit", "webSearch": "WebSearch"}.get(
                            kind, "mcp__codex__tool")
                        on_tool(tool, {"command": what} if kind == "commandExecution" else {})
                        if kind == "webSearch" and not said and not already():
                            speak(self.holding[0])
                elif method == "turn/completed":
                    speak(pending[0].strip())
                    turn = p.get("turn") or {}
                    if turn.get("status") == "failed":
                        raise RuntimeError(f"Codex: {(turn.get('error') or {}).get('message', 'the turn failed')}")
                    return " ".join(said), used
                elif method == "error":
                    err = p.get("error") or p
                    if not p.get("willRetry"):
                        raise RuntimeError(f"Codex: {err.get('message', err)}")
                elif method == "codex/exited":
                    raise RuntimeError("Codex stopped running.")
        except asyncio.CancelledError:  # the user's "stop": tell Codex to stop too
            if turn_id:
                try:
                    await asyncio.wait_for(self.request("turn/interrupt", {"threadId": thread, "turnId": turn_id}), 5)
                except Exception:  # noqa: BLE001 - stopping regardless
                    pass
            raise
        finally:
            self.listeners.pop(thread, None)

    async def ask(self, text, meanwhile=lambda said: None, model=None, already=lambda: False,
                  on_tool=lambda name, args: None, picture=None):
        start = time.perf_counter()
        if self.fresh:
            self.thread, self.fresh = await self._new_thread(self.system), False
        effort = EFFORT["deep" if (model or "").endswith("deep") else "fast" if (model or "").endswith("fast") else "work"]
        first = []
        on = f"\n\n[answering on {self.model}, {effort} effort]" if getattr(self, "model", None) else ""
        answer, used = await self._turn(self.thread, self.context + text + on,
                                        effort, lambda s: (first or first.append(time.perf_counter()), meanwhile(s)),
                                        already, on_tool, picture)
        self.context = ""
        seconds = round(time.perf_counter() - start, 2)
        self.turns.append((text, answer))
        self.log({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "you": text, "jugnu": answer,
                  "model": [f"codex ({effort})"], "consulted": used, "seconds": seconds,
                  "first_words": round(first[0] - start, 2) if first else None, "error": False})
        return "", seconds

    async def quick(self, extra, prompt):
        """The side voice: its own read-only thread, quick effort, nothing spoken from here."""
        if self.side_thread is None:
            self.side_thread = await self._new_thread(self.base_system + extra, read_only=True)
        answer, _ = await self._turn(self.side_thread, prompt, "low", lambda s: None)
        return answer.strip()


CODEX_HAS = {"read_file", "list_folder", "find_files", "search_in_files", "write_file", "edit_file", "run_command",
             "fetch_page", "web_search"}  # what Codex brings itself: the rest of an Agent's tools are handed to it
CODEX_RULES = ("You are thinking on GPT through Codex. You can read and edit the user's files, run commands and "
               "search the web. Jugnu adds tools of its own when they're set up: the council (ask_...), PubMed "
               "(search_pubmed, read_abstract: for the literature use these, never the web), the browser "
               "(browser__...) and the user's Google Drive and Gmail (google__...). If you run inside WSL, their Windows drives are under /mnt (C:\\ is /mnt/c). Commands Codex doesn't already trust are checked first, and risky ones (deleting, moving, "
               "stopping programs, system changes, touching password or key files) wait for their spoken OK; if they "
               "say no, don't try another way round. Before a search, or reading something long, say a short "
               "line first. No markdown, lists or headings: everything you write is spoken.")


if __name__ == "__main__":  # no keys, no network: python brain.py
    page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa%3Fb%3D1&amp;rut=x">'
            'Why <b>fireflies</b> glow</a><a class="result__snippet" href="#">Light from <b>luciferin</b> &amp; oxygen.</a>'
            '<a class="result__a" href="https://duckduckgo.com/y.js?ad=1">An advert</a><a class="result__snippet">Buy.</a>')
    assert results(page) == "1. Why fireflies glow\n   https://example.org/a?b=1\n   Light from luciferin & oxygen.", results(page)
    assert results("<html>nothing here</html>") == ""
    cfg = {"name": "Test", "dialect": "openai", "provider": "custom", "base": "http://x", "key": None,
           "fast": "a", "work": "b", "deep": "b"}
    names = lambda agent: [t["name"] for t in agent.tools()]
    a = Agent("s", cfg=cfg)
    assert {"web_search", "read_file", "run_command", "search_pubmed"} <= set(names(a))
    os.environ.setdefault("GEMINI_API_KEY", "k")  # only so the Gemini kind can be made: nothing is sent
    assert "web_search" not in names(Agent("s", cfg={**cfg, "dialect": "gemini", "key": "GEMINI_API_KEY"}))  # it has Google Search
    reader = Agent("s", cfg=cfg, read_only=True)
    assert "run_command" not in names(reader) and "read_file" in names(reader)
    a.chat.user("look", "QUJD"); a.chat.unsee(0, " (gone)")  # a picture is seen once, then taken back out
    assert a.chat.history == [{"role": "user", "content": "look (gone)"}], a.chat.history
    g = GeminiNative({**cfg, "key": "GEMINI_API_KEY"}); g.user("look", "QUJD"); g.unsee(0)
    assert g.history == [{"role": "user", "parts": [{"text": "look"}]}], g.history
    assert as_claude("web_search", {"query": "q"}) == ("WebSearch", {"query": "q"})
    assert "ask_xai" in HANDS_ON and "ask_custom" not in HANDS_ON  # Grok works hands-on; an "other server" answers only
    codex = CodexAgent("s", cfg={"name": "GPT", "model": None}, command=["codex", "app-server"])
    own = [t["name"] for t in codex.hands.tools() if t["name"] not in CODEX_HAS]  # what GPT on a plan is handed
    assert "search_pubmed" in own and not set(own) & CODEX_HAS and "read_file" not in own, own
    print("ok")
