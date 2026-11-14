"""Jugnu's council: other models Claude can consult.

Each tool's description is its job description. Claude reads them and
decides who to ask. Every outbound call is printed and logged.
"""
import asyncio
import contextlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

# read .env before the settings below (jugnu.py imports this file before it loads .env)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from claude_agent_sdk import create_sdk_mcp_server, tool

import auditors

# Anything the user must not send to other companies' models (say, work under an NDA),
# described in plain words in .env as JUGNU_NO_SHARE, e.g. "my day job at Acme".
# Empty: everything may go to every adviser.
NO_SHARE_WHAT = os.getenv("JUGNU_NO_SHARE", "").strip()
NO_SHARE = (f" Never send anything from {NO_SHARE_WHAT}. Follow this silently: never say you checked,"
            " or that something is fine to share; mention it only if that material is actually involved."
            ) if NO_SHARE_WHAT else ""

# Who sits on the council: JUGNU_COUNCIL, comma-separated (the setup screen writes it). Default: GPT, Gemini, DeepSeek.
# The brain never sits on its own council.
_brain = os.getenv("JUGNU_BRAIN", "claude").strip().lower()
SELF = {"openai": "gpt"}.get(_brain, _brain)
MEMBERS = [m.strip().lower() for m in os.getenv("JUGNU_COUNCIL", "gpt,gemini,deepseek").split(",")
           if m.strip() and m.strip().lower() != SELF]
CLAUDE_MODEL = os.getenv("JUGNU_COUNCIL_CLAUDE", "opus")

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")  # thinks by default
# The council seats each family's strongest model. Gemini's, strongest first: the first one your key can use
# answers (Carbon and Argon need a paid key; Hydrogen also answers on the free tier).
GEMINI_LADDER = ["gemini-4-carbon", "gemini-4-argon", "gemini-4-hydrogen", "gemini-3.8-flash"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", GEMINI_LADDER[0])
# When you tell it "use Argon for Gemini", that choice is kept here and outlasts restarts.
CHOSEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "council_models.json")
try:
    _chosen = json.load(open(CHOSEN))
    GEMINI_MODEL = _chosen.get("gemini", GEMINI_MODEL)
    DEEPSEEK_MODEL = _chosen.get("deepseek", DEEPSEEK_MODEL)
except (OSError, ValueError):
    pass


def _post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # surface the provider's reason (bad key, wrong model)
        msg = e.read().decode(errors="replace")
        for v in headers.values():  # some providers echo the key back; keep it out of logs
            msg = msg.replace(v.split()[-1], "***")
        raise RuntimeError(f"HTTP {e.code}: {msg[:300]}") from None


def _deepseek(prompt):
    data = _post(
        "https://api.deepseek.com/chat/completions",
        {"Content-Type": "application/json",
         "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
        {"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}]},
        timeout=180,  # thinking takes a while
    )
    return data["choices"][0]["message"]["content"]  # the answer, not its reasoning


# Seats on any OpenAI-style API, like the brains of the same names: seat -> (name, key in .env, address, model in .env)
CHAT_SEATS = {
    "xai": ("Grok", "XAI_API_KEY", "https://api.x.ai/v1", "COUNCIL_XAI_MODEL"),
    "openrouter": ("OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "COUNCIL_OPENROUTER_MODEL"),
    "ollama": ("Local model", None, "http://localhost:11434/v1", "COUNCIL_OLLAMA_MODEL"),
    "custom": ("Other server", "COUNCIL_CUSTOM_KEY", os.getenv("COUNCIL_CUSTOM_URL", ""), "COUNCIL_CUSTOM_MODEL"),
}


def _chat(seat, prompt):
    name, key, base, model = CHAT_SEATS[seat]
    headers = {"Content-Type": "application/json"}
    if key and os.getenv(key):
        headers["Authorization"] = f"Bearer {os.environ[key]}"
    data = _post(base.rstrip("/") + "/chat/completions", headers,
                 {"model": os.getenv(model, ""), "messages": [{"role": "user", "content": prompt}]}, timeout=300)
    return data["choices"][0]["message"]["content"] or ""


def _gemini(prompt, model=None, timeout=180):
    """model: that one only. Without it, the council's Gemini; if this key can't use it, the next one down the
    ladder that it can, which is then kept for the session."""
    global GEMINI_MODEL
    rest = GEMINI_LADDER[GEMINI_LADDER.index(GEMINI_MODEL) + 1:] if GEMINI_MODEL in GEMINI_LADDER else []
    for m in [model] if model else [GEMINI_MODEL, *rest]:
        try:
            data = _post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                {"contents": [{"parts": [{"text": prompt}]}]},
                timeout=timeout,
            )
            break
        except RuntimeError as e:  # not on this key (a free one, or a model since retired): the next strongest
            if model or m == ([GEMINI_MODEL, *rest])[-1] or not re.search(r"HTTP (403|404)|free.tier|limit: 0", str(e), re.I):
                raise
            print(f"  (Gemini: this key can't use {m}; trying the next strongest)", flush=True)
    if not model:
        GEMINI_MODEL = m
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _gemini_models():
    """The Gemini models this key can actually use, newest names first, from Google itself."""
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                                 headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        models = json.load(r).get("models", [])
    names = sorted((m["name"].removeprefix("models/") for m in models
                    if "generateContent" in m.get("supportedGenerationMethods", [])), reverse=True)
    return names


def claude_cli():
    """The Claude Code inside the Agent SDK (installed with Jugnu), else one on PATH."""
    import shutil
    import claude_agent_sdk
    exe = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled",
                       "claude.exe" if os.name == "nt" else "claude")
    return exe if os.path.exists(exe) else shutil.which("claude") or "claude"


def _claude(prompt, files=()):
    # Claude Code on your Claude plan (its own sign-in), no tools: a second opinion only. It runs from an
    # empty temp folder, so it can't see this one (and the keys in .env).
    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty:  # Windows may still hold it: the answer matters, the empty folder doesn't
        prompt += _bring(files, empty)
        r = subprocess.run([claude_cli(), "-p", "--tools", "Read,Glob,Grep" if files else "", "--model", CLAUDE_MODEL,
                            "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=empty, timeout=900 if files else 300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-500:] or f"claude exited {r.returncode}")
    return r.stdout.strip()


def _gpt(prompt, files=()):
    # Codex on your ChatGPT login: the one on PATH (npm install -g @openai/codex), else the one inside
    # WSL. It runs read-only from an empty folder, so it can't see this one (and the keys in .env).
    import shutil
    import tempfile
    codex = shutil.which("codex")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty:  # Windows may still hold it: the answer matters, the empty folder doesn't
        # ponytail: Codex inside WSL starts in WSL's own /tmp, which the copies aren't in: it gets their text instead
        prompt += _bring(files, empty) if codex else shown(files)
        r = subprocess.run(
            [codex, "exec", "--skip-git-repo-check", "--ephemeral", "-"] if codex else
            ["wsl.exe", "-e", "bash", "-lc", "cd /tmp && codex exec --skip-git-repo-check --ephemeral -"],
            input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=empty, timeout=900 if files else 180,
        )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-500:] or f"codex exited {r.returncode}")
    return r.stdout.strip()


CONSULTING = []  # advisers being asked right now; Jugnu's face lights its core in their colors


@contextlib.contextmanager
def consulting(name):
    CONSULTING.append(name.lower())
    try:
        yield
    finally:
        CONSULTING.remove(name.lower())


MAX_SHOWN = 400_000  # characters of documents sent with one question (roughly 100,000 tokens)


def _paths(files):
    """The documents named for the council, as full paths: a list, or one per line. ValueError, in words for
    the brain, if one can't be shown."""
    import guard
    paths = [os.path.expanduser(p.strip().strip('"'))
             for p in (re.split(r"[\n;|]+", files) if isinstance(files, str) else files or []) if p.strip()]
    for path in paths:
        if guard.SECRETS.search(path):
            raise ValueError(f"{path} looks like a password or key file: those are never shown to the council")
        if not os.path.isfile(path):
            raise ValueError(f"there is no file at {path}: give the documents' full paths")
    return paths


SEAT_OF = {"GPT": "gpt", "Claude": "claude", "Gemini": "gemini", "DeepSeek": "deepseek",
           **{name: seat for seat, (name, *_) in CHAT_SEATS.items()}}
READER = ("You are {name}, sitting on a council that has been asked for a second opinion. The documents named in "
          "the question are on the user's computer: open and read them yourself with read_file before you answer. "
          "They are all you can open, and you can change or run nothing. Answer plainly and completely: your reply "
          "goes to the assistant who asked, who reports to the user.")


async def _reader(name, question, paths):
    """A member reading for itself, as a brain does: the same reading tools, limited to the documents it was shown."""
    import brain  # here, not at the top: brain imports this module
    real = [os.path.realpath(p) for p in paths]

    async def gate(tool, inp):
        asked = os.path.realpath(os.path.expanduser(inp.get("file_path") or inp.get("path") or "."))
        return None if asked in real else "you were shown only these: " + "; ".join(paths)
    said = []
    member = brain.Agent(READER.format(name=name), gate=gate, read_only=True, cfg=brain.guest(SEAT_OF[name]), holding=("", ""))
    member.alone = True  # no council of its own to ask
    await asyncio.wait_for(member.ask(question, meanwhile=lambda s: s and said.append(s), model=member.cfg["deep"]),
                           auditors_minutes() * 60)
    return " ".join(said)


def _bring(files, folder):
    """Copies of the documents in a member's own empty folder (all it can see); the line telling it they're there."""
    import shutil
    names = []
    for i, path in enumerate(files):
        name = os.path.basename(path)
        name = name if name not in names else f"{i + 1} {name}"  # two with one name: both kept
        shutil.copy2(path, os.path.join(folder, name))
        names.append(name)
    return ("\n\n[The documents to read, yourself, before you answer, are in the current folder: "
            + "; ".join(names) + ". Read them; change and run nothing.]") if names else ""


def shown(files):
    """The documents as text to add to a question, for a member that can't open them itself."""
    import argon  # here, not at the top: argon imports this module
    out, room = [], MAX_SHOWN
    for path in _paths(files):
        text = argon.read_file(path)[:room]
        room -= len(text)
        out.append(f"--- {os.path.basename(path)} ---\n{text}")
    return "\n\n[The documents to read:\n\n" + "\n\n".join(out) + "\n]" if out else ""


async def _consult(name, fn, args, needs=None):
    if needs and not os.getenv(needs):
        return {"content": [{"type": "text", "text": f"{name} isn't set up ({needs} missing)."}],
                "is_error": True}
    folder = (args.get("folder") or "").strip().strip('"')
    files = args.get("files") or []
    model = {"Gemini": GEMINI_MODEL, "DeepSeek": DEEPSEEK_MODEL, "Claude": CLAUDE_MODEL,
             **{n: os.getenv(m, "") for n, _, _, m in CHAT_SEATS.values()}}.get(name, "codex")
    print(f"  → asking {name} ({model}){', hands-on in ' + folder if folder else ', with documents to read' if files else ''}...",
          flush=True)
    start = time.perf_counter()
    try:
        with consulting(name):
            if folder:  # its own container, with a copy of the folder (auditors.py)
                if name == "GPT":
                    text, out = await asyncio.to_thread(auditors.gpt, args["question"], folder, auditors_minutes())
                else:
                    text, out = await asyncio.to_thread(auditors.run, name, args["question"], folder, model,
                                                        auditors_minutes())
                if out:
                    written = out / auditors.findings(name)
                    text += (f"\n\n[{name}'s full findings: {written}]" if written.exists()
                             else f"\n\n[{name} wrote no findings file; its transcript is in {out}]")
            elif files:  # documents to read: each member opens them itself, as a brain would; nothing is run
                paths = _paths(files)
                if name in ("Claude", "GPT"):
                    text = await asyncio.to_thread(_claude if name == "Claude" else _gpt, args["question"], paths)
                else:
                    listed = "\n\n[The documents to read, yourself, before you answer:\n" + "\n".join(paths) + "]"
                    try:
                        text = await _reader(name, args["question"] + listed, paths)
                    except (ValueError, KeyError) as e:  # it can't be set up to read for itself: their text goes along
                        print(f"  ({name} can't open files itself: {str(e)[:120]}; sending their text)", flush=True)
                        text = await asyncio.to_thread(fn, args["question"] + shown(paths))
            else:
                text = await asyncio.to_thread(fn, args["question"])
    except Exception as e:  # report failures to Claude instead of crashing
        print(f"  ✗ {name} failed after {time.perf_counter() - start:.1f}s: {str(e)[:160]}", flush=True)
        return {"content": [{"type": "text", "text": f"{name} failed: {e}"}], "is_error": True}
    print(f"  ← {name} answered in {time.perf_counter() - start:.1f}s", flush=True)
    return {"content": [{"type": "text", "text": text}]}


def auditors_minutes():
    return float(os.getenv("JUGNU_AGENT_MINUTES", "30"))


def council_minutes():
    """The longest a hands-on council job can take: members queue (auditors.AT_ONCE), and GPT may need a second model."""
    return auditors_minutes() * (-(-max(1, len(MEMBERS)) // auditors.AT_ONCE) + 1)


FILES = {"type": "array", "items": {"type": "string"}, "description": (
    "Optional: documents for it to read with the question, by full path (reports, PDFs, Word and text files). "
    "It opens and reads them itself, the way you read a file, and can open nothing else; nothing is run. This is "
    "how to show the council a report, a run's results or a document and ask what it thinks; never send a "
    "summary of them instead.")}


def _schema(who):
    return {"type": "object", "required": ["question"], "properties": {
        "question": {"type": "string", "description": "The question, or the task to do hands-on."},
        "files": FILES,
        "folder": {"type": "string", "description": (
            f"Optional: a folder for {who} to work on hands-on, in its own sandboxed container holding a copy "
            "of it: reads every file, installs what's needed, runs the code and tests on the GPU, and writes "
            "a findings file. Give it only when the code itself is to be run, tested or audited; to have reports "
            "or documents read, give files instead. Never send a summary in place of either. Takes many minutes.")}}}


HANDS = (" Give files and it opens and reads those documents itself, running nothing. Give a folder only when "
         "code is to be run or audited: it then works on a copy hands-on in its own sandboxed container; for an audit "
         "by the council, give every member the folder in the same turn; they take turns on the machine by themselves.")


@tool("ask_gpt", "GPT via Codex. An independent second opinion, especially on "
      "code, math and logic. Use when a mistake would be costly or the user asks "
      "for a cross-check. Slow (10-60s)." + HANDS + NO_SHARE, _schema("GPT"))
async def ask_gpt(args):
    return await _consult("GPT", _gpt, args)


@tool("ask_claude", "Claude (Anthropic), through Claude Code on the user's Claude plan. A careful second "
      "opinion on reasoning, writing, code and science. Slow (10-60s)." + HANDS + NO_SHARE, _schema("Claude"))
async def ask_claude(args):
    return await _consult("Claude", _claude, args)


@tool("ask_gemini", "Gemini. Good with very long texts and broad general "
      "knowledge. Use for a second perspective, especially on long material." + HANDS + NO_SHARE,
      _schema("Gemini"))
async def ask_gemini(args):
    return await _consult("Gemini", _gemini, args, needs="GEMINI_API_KEY")


@tool("ask_deepseek", "DeepSeek V4 Pro, a reasoning model. Strong on math, code "
      "and step-by-step logic; a third independent view when GPT and Gemini "
      "disagree. Can take a minute." + HANDS + NO_SHARE, _schema("DeepSeek"))
async def ask_deepseek(args):
    return await _consult("DeepSeek", _deepseek, args, needs="DEEPSEEK_API_KEY")


@tool("gemini_models", "Lists the Gemini models the user's key can use right now, straight from "
      "Google, and which one the council uses. Use this, not your memory or Gemini's own answer, when "
      "they ask which Gemini models exist or whether a model (e.g. Argon) is available.", {})
async def gemini_models(args):
    if not os.getenv("GEMINI_API_KEY"):
        return {"content": [{"type": "text", "text": "Gemini isn't set up (GEMINI_API_KEY missing)."}], "is_error": True}
    try:
        names = await asyncio.to_thread(_gemini_models)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Couldn't list Gemini models: {e}"}], "is_error": True}
    return {"content": [{"type": "text", "text": f"Council's Gemini now: {GEMINI_MODEL}. "
                         f"Available on this key ({len(names)}): " + ", ".join(names)}]}


@tool("use_model", "Changes which model a council member uses, when the user asks (\"use Argon for "
      "Gemini\", \"switch Gemini to 3.8 Flash\"). member: gemini or deepseek. model: the exact model id; "
      "for Gemini, check gemini_models first and pick the matching id. The choice outlasts restarts. "
      "GPT can't be changed here: Codex picks its own model.", {"member": str, "model": str})
async def use_model(args):
    global GEMINI_MODEL, DEEPSEEK_MODEL
    member, model = args["member"].strip().lower(), args["model"].strip().removeprefix("models/")
    if member not in ("gemini", "deepseek"):
        return {"content": [{"type": "text", "text": "Only gemini or deepseek can be changed."}], "is_error": True}
    if member == "gemini":  # a quick hello first: don't switch to a name that doesn't answer
        try:
            await asyncio.to_thread(_gemini, "Reply with just the word OK.", model, 45)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"{model} didn't answer, so Gemini stays on "
                                 f"{GEMINI_MODEL}: {e}"}], "is_error": True}
        GEMINI_MODEL = model
    else:
        DEEPSEEK_MODEL = model
    try:
        os.makedirs(os.path.dirname(CHOSEN), exist_ok=True)
        json.dump({"gemini": GEMINI_MODEL, "deepseek": DEEPSEEK_MODEL}, open(CHOSEN, "w"))
    except OSError:
        pass
    print(f"  (council: {member} now uses {model})", flush=True)
    return {"content": [{"type": "text", "text": f"Done: {member} now uses {model}."}]}


HANDS_ON = {"xai", "openrouter", "ollama"}  # chat seats with hands in the container: Codex drives Grok, Claude Code the others


def _chat_seat(seat):
    name, key, _, model = CHAT_SEATS[seat]

    async def ask(args):
        if args.get("folder") and seat not in HANDS_ON:  # ponytail: an "other server" answers only; hands for it once one is asked for
            args = {**args, "folder": ""}
        return await _consult(name, lambda q: _chat(seat, q), args, needs=key or model)
    if seat in HANDS_ON:
        return tool(f"ask_{seat}", f"{name} ({os.getenv(model, '')}). Another independent second opinion."
                    + HANDS + NO_SHARE, _schema(name))(ask)
    return tool(f"ask_{seat}", f"{name} ({os.getenv(model, '')}). Another independent second opinion. Answers "
                "questions, and reads documents you give as files; it can't work on a folder hands-on." + NO_SHARE,
                {"type": "object", "required": ["question"],
                 "properties": {"question": {"type": "string", "description": "The question."}, "files": FILES}})(ask)


SEATS = [t for who, t in (("claude", ask_claude), ("gpt", ask_gpt), ("gemini", ask_gemini), ("deepseek", ask_deepseek)) if who in MEMBERS]
SEATS += [_chat_seat(s) for s in CHAT_SEATS if s in MEMBERS and os.getenv(CHAT_SEATS[s][3])]
SERVER = create_sdk_mcp_server("council", tools=SEATS + [gemini_models, use_model])
TOOL_NAMES = [f"mcp__council__{t.name}" for t in SEATS] + ["mcp__council__gemini_models", "mcp__council__use_model"]


if __name__ == "__main__":
    # Which Gemini models answer on your key? Strongest first; the council uses the first OK one by itself.
    # Jugnu never runs this; you do: python council.py
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
    names = _gemini_models()
    print(f"{len(names)} Gemini models on your key:", ", ".join(names), "\n")
    for m in GEMINI_LADDER:
        start = time.perf_counter()
        try:
            out = _gemini("Reply with just the word OK.", model=m, timeout=30).strip()
            print(f"OK    {m:24} {time.perf_counter() - start:4.1f}s  {out[:20]!r}")
        except Exception as e:
            print(f"no    {m:24} {time.perf_counter() - start:4.1f}s  {' '.join(str(e).split())[:110]}")
