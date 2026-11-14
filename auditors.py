"""The council, hands-on: each member works in its own Docker container.

When Claude gives a council member a folder, that member gets a throwaway Linux container
(the jugnu-auditor image, see auditor/Dockerfile) holding a copy of the folder and nothing
else from this machine: not your drives, not Jugnu's .env, not your real folder. Inside it
the member is fully autonomous: reads everything, installs what the project needs, runs the
code and tests on your GPU. Big folders (model weights) are mounted read-only rather than
copied; Windows venvs are left out (they can't run on Linux: the member builds its own).

What comes back to Documents\\Jugnu workspaces\\<job>\\: the member's findings file and a
transcript of its run. Then the container and its copy are deleted.

Hands: Claude drives Claude Code, GPT drives Codex, Gemini drives Gemini CLI. DeepSeek, OpenRouter and a
local model drive Claude Code pointed at their own servers; Grok drives Codex pointed at xAI's. Each
container gets only its own member's key.

    python auditors.py build   build the image once (~10 GB, ~10 min)
    python auditors.py check   each member runs a one-line task in a container
"""
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGE = os.getenv("JUGNU_AUDITOR_IMAGE", "jugnu-auditor")
WORKSPACES = Path(os.getenv("JUGNU_WORKSPACES", Path.home() / "Documents" / "Jugnu workspaces"))
CACHE = "jugnu-auditor-cache"  # one Docker volume for pip/uv downloads, shared by all runs
# OpenAI's models, in order. One that isn't on your plan passes to the next; one that declines
# on safety grounds ends GPT's part in this job (it sits it out, and the report says so).
GPT_MODELS = [m.strip() for m in os.getenv("JUGNU_GPT_MODELS", "gpt-6-astra,gpt-6.1-sol,gpt-5.6-sol").split(",")
              if m.strip()]

LINK = re.compile(r"^(?:models?|weights|checkpoints|data/raw)$", re.I)       # mounted read-only, not copied
VENV = re.compile(r"^(?:\.?venv.*|env|\.conda|node_modules)$", re.I)          # Windows-only: left out
JUNK = ["__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".ipynb_checkpoints"]
SECRETS = [".env", ".env.*", "*.pem", "id_rsa*", "id_ed25519*", "credentials.json", "secrets.*"]
BIG = 500 * 1024 ** 2  # any folder bigger than this is mounted read-only rather than copied

BLOCKED = re.compile(r"biological|bio.?risk|cyber.?risk|usage polic|safety (?:system|filter|reasons)|flagged", re.I)
DECLINED = re.compile(r"\b(?:I|we)(?:'m| am)? (?:can.?t|cannot|won.?t|unable to|not able to) "
                      r"(?:help|assist|do that|provide|comply)", re.I)
UNAVAILABLE = re.compile(r"model[^\n]{0,80}(?:not found|does not exist|not available|not supported|unsupported|"
                         r"no access|invalid|unknown)", re.I)


def docker(*args, timeout=900, input=None, env=None):
    r = subprocess.run(["docker", *args], input=input, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"docker {args[0]} failed").strip()[-600:])
    return r.stdout


DESKTOP = os.getenv("JUGNU_DOCKER_DESKTOP", r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
_starting = threading.Lock()  # three members asking at once start Docker once
AT_ONCE = max(1, int(os.getenv("JUGNU_AUDITORS_AT_ONCE", "1")))  # members working at the same time
_bench = threading.BoundedSemaphore(AT_ONCE)  # the rest wait their turn
STATUS = {}        # who -> {"state": waiting|working|done|stopped, "since", "job", "took"}: for progress()
_stopped = [0.0]   # when the user last said stop: runs asked for before then end (stop_all)


def running():
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


def ensure_docker(wait=180):
    """Start Docker Desktop if it isn't running, and wait for its engine (a minute or so)."""
    with _starting:
        if running():
            return
        if not os.path.exists(DESKTOP):
            raise RuntimeError(f"Docker isn't running, and Docker Desktop isn't at {DESKTOP} "
                               "(set JUGNU_DOCKER_DESKTOP in .env)")
        print("  (starting Docker Desktop...)", flush=True)
        subprocess.Popen([DESKTOP], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        end = time.monotonic() + wait
        while time.monotonic() < end:
            time.sleep(3)
            if running():
                print("  (Docker is up)", flush=True)
                return
        raise RuntimeError(f"Docker Desktop didn't come up within {wait // 60} minutes")


def bigger(folder, limit=BIG):
    total = 0
    for root, _, files in os.walk(folder):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if total > limit:
                return True
    return False


def findings(who):
    return f"{who} findings.md"


def brief(who, src, mounted, task):
    ro = ", ".join(f"/work/{m}" for m in mounted) or "none"
    return (f"You're working in a disposable Linux container made for you alone. /work holds a copy of the "
            f"user's folder {src}; nothing else from their machine is here, so work freely: read everything, "
            f"install what the project needs (uv and a Python with CUDA PyTorch are ready; the internet is "
            f"open for packages), and run the code and tests. An NVIDIA GPU is available"
            f"{' but shared with other reviewers working at the same time: use it when it has room, and' if AT_ONCE > 1 else ';'}"
            f" if CUDA runs out of memory, run on the CPU instead (there are plenty of cores and RAM) rather than give up. Read-only, shared "
            f"with other runs: {ro}. Their Windows virtual environments were left out; build your own if the "
            f"project needs one. Take the time the job needs. Run things rather than guess, and back each "
            f"claim with what you ran and what it printed. Write your full findings to /work/{findings(who)}: "
            f"ranked by severity, each with the file and line, what's wrong, why it matters, the evidence and "
            f"the fix; then what you couldn't check, and why. End with a short summary as your reply.\n\n"
            f"The task:\n{task}\n")


def _codex_auth():
    """GPT's container needs your ChatGPT login for Codex: the auth file, and only for that container."""
    for home in (os.getenv("CODEX_HOME"), Path.home() / ".codex"):
        if home and (Path(home) / "auth.json").exists():
            return (Path(home) / "auth.json").read_text(encoding="utf-8")
    raise RuntimeError("no Codex login found (~/.codex/auth.json): sign in to Codex on Windows once")


def _claude_token():
    creds = Path(os.getenv("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / ".credentials.json"
    try:
        return json.loads(creds.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError):
        raise RuntimeError("no Claude sign-in found: sign in to Claude in setup first") from None


def hands(who, model):
    """The command that runs inside the container, and the secrets it gets (by name; Docker reads
    the values from this process's environment, so they never appear on a command line)."""
    task = "Do the task described in /work/TASK.md."
    if who == "GPT":
        return (f'mkdir -p ~/.codex && printf %s "$CODEX_AUTH" > ~/.codex/auth.json && '
                f"codex exec --skip-git-repo-check --sandbox danger-full-access -m {shlex.quote(model)} "
                f"{shlex.quote(task)}"), {"CODEX_AUTH": _codex_auth()}
    if who == "Claude":  # your Anthropic API key, else your Claude Code sign-in's current access token
        # (only the access token: the container can't refresh it, so it can't sign Windows out)
        secret = ({"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]} if os.getenv("ANTHROPIC_API_KEY")
                  else {"CLAUDE_CODE_OAUTH_TOKEN": os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or _claude_token()})
        return (f"claude -p {shlex.quote(task)} --dangerously-skip-permissions --model {shlex.quote(model)}", secret)
    if who == "Grok":  # xAI speaks OpenAI's Responses API (its Anthropic one is retired), so Codex drives it, on your xAI key
        provider = 'model_providers.xai={name="xAI",base_url="https://api.x.ai/v1",env_key="XAI_API_KEY",wire_api="responses"}'
        return (f"codex exec --skip-git-repo-check --sandbox danger-full-access -c model_provider=xai "
                f"-c {shlex.quote(provider)} -m {shlex.quote(model)} {shlex.quote(task)}",
                {"XAI_API_KEY": os.environ["XAI_API_KEY"]})
    if who == "Gemini":
        return (f"gemini -m {shlex.quote(model)} --yolo -p {shlex.quote(task)}",
                {"GEMINI_API_KEY": os.environ["GEMINI_API_KEY"],
                 "GEMINI_CLI_TRUST_WORKSPACE": "true"})  # headless: no "trust this folder?" question
    # DeepSeek, OpenRouter and Ollama speak Anthropic's API too, so Claude Code drives them, on their model
    base, token = {
        "DeepSeek": ("https://api.deepseek.com/anthropic", lambda: os.environ["DEEPSEEK_API_KEY"]),
        "OpenRouter": ("https://openrouter.ai/api", lambda: os.environ["OPENROUTER_API_KEY"]),
        # Ollama on Windows, seen from the container (it must listen beyond localhost: OLLAMA_HOST=0.0.0.0)
        "Local model": (os.getenv("JUGNU_OLLAMA_FROM_DOCKER", "http://host.docker.internal:11434"), lambda: "ollama"),
    }.get(who, (None, None))
    if not base:
        raise ValueError(who)
    return (f"claude -p {shlex.quote(task)} --dangerously-skip-permissions --model {shlex.quote(model)}",
            {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": token(), "ANTHROPIC_API_KEY": "",
             "ANTHROPIC_MODEL": model, "ANTHROPIC_SMALL_FAST_MODEL": model,
             "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
             "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})


def run(who, task, folder, model, minutes=30):
    """One member, hands-on, in its own container. Returns (its reply, the folder its findings went to)."""
    asked = time.time()
    STATUS[who] = status = {"state": "waiting", "since": asked}
    if not _bench.acquire(blocking=False):
        print(f"  ({who} waits its turn)", flush=True)
        _bench.acquire()
    try:
        if _stopped[0] > asked:
            raise RuntimeError("the user stopped the job before this member started")
        status.update(state="working", since=time.time())
        reply, out = _run(who, task, folder, model, minutes, status)
        stopped = _stopped[0] > asked
        status.update(state="stopped" if stopped else "done", took=time.time() - status["since"])
        return ("(the user stopped the job while it was working)" if stopped else reply), out
    except BaseException:
        status.update(state="stopped" if _stopped[0] > asked else "failed", took=time.time() - status["since"])
        raise
    finally:
        _bench.release()


def stop_all():
    """The user said stop: kill the members' running containers; those still waiting won't start."""
    _stopped[0] = time.time()
    for status in list(STATUS.values()):
        if status.get("job") and status["state"] == "working":
            subprocess.run(["docker", "kill", status["job"]], capture_output=True)


# what a member is doing, from the processes running in its container (any CLI, no log parsing)
ACTIVITY = [(r"pytest|unittest|\btox\b", "running the tests"),
            (r"\b(?:pip|uv|apt-get|npm|conda)\b.*\b(?:install|sync|add)\b", "installing packages"),
            (r"python\S*\s+(?:-\S+\s+)*(\S+\.py)\b", "running {}"),
            (r"\b(?:nvcc|make|cmake|gcc)\b", "building code")]


def activity(job):
    try:
        procs = subprocess.run(["docker", "top", job, "-eo", "args"], capture_output=True, text=True,
                               timeout=10).stdout.splitlines()[1:]
    except (OSError, subprocess.TimeoutExpired):
        return "working"
    for pattern, what in ACTIVITY:
        for proc in reversed(procs):  # newest last
            if m := re.search(pattern, proc):
                return what.format(Path(m.group(1)).name) if m.groups() else what
    return "reading and thinking" if procs else "getting its copy ready"


def progress():
    """Where the council is, said out loud: ("GPT finished in 2 minutes. Gemini is running the
    tests, 4 minutes in.", a signature without the minutes, to tell real news from the clock)."""
    mins = lambda t: f"{max(1, round(t / 60))} minute{'s' if round(t / 60) > 1 else ''}"
    said, sig = [], []
    for who, s in list(STATUS.items()):
        if s["state"] == "waiting":
            what = "is waiting its turn"
        elif s["state"] == "working":
            doing = activity(s["job"]) if s.get("job") else "getting its copy ready"
            what = f"is {doing}"
            sig.append(f"{who} {what}")
            what += f", {mins(time.time() - s['since'])} in"
        else:
            ended = {"stopped": "stopped", "failed": "failed"}.get(s["state"], "finished")  # a failure isn't "finished"
            what = f"{ended} after {mins(s.get('took', 0))}"
        said.append(f"{who} {what}")
        if s["state"] != "working":
            sig.append(said[-1])
    return " ".join(x + "." for x in said), "|".join(sig)


def _run(who, task, folder, model, minutes, status):
    src = Path(folder).expanduser().resolve()
    if not src.is_dir():
        raise RuntimeError(f"there's no folder at {src}")
    ensure_docker()
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        raise RuntimeError(
            f"the {IMAGE} image, the sandbox the council works in, isn't built yet: a one-time build of about 10 GB "
            "that takes 10 minutes or more. If the user says to build it, run this, which opens a window of its own "
            f"for the build and returns at once (in PowerShell put & in front): \"{sys.executable}\" "
            f"\"{Path(__file__).resolve()}\" window  Then wait for them to say the window reports it is built.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    job = f"jugnu-{who.lower()}-{stamp}"
    out = WORKSPACES / f"{src.name} - {who} - {stamp}"
    out.mkdir(parents=True)
    dirs = [p for p in src.iterdir() if p.is_dir()]
    mounted = [p.name for p in dirs if not VENV.match(p.name) and (LINK.match(p.name) or bigger(p))]
    left_out = [p.name for p in dirs if VENV.match(p.name)] + mounted
    excludes = [f"--exclude=./{n}" for n in left_out] + [f"--exclude={p}" for p in JUNK + SECRETS]

    status["job"] = job
    docker("volume", "create", job)
    try:
        # the copy: tar from a read-only view of your folder into the job's own volume (fast storage)
        docker("run", "--rm", "--user", "root", "-v", f"{src}:/src:ro", "-v", f"{job}:/work", "--entrypoint", "bash", IMAGE, "-c",
               f"tar -C /src {' '.join(map(shlex.quote, excludes))} -cf - . | tar -C /work -xf - "
               f"&& chown -R auditor /work", timeout=1800)
        docker("run", "--rm", "-i", "-v", f"{job}:/work", "--entrypoint", "bash", IMAGE, "-c", "cat > /work/TASK.md",
               input=brief(who, src, mounted, task))
        command, secrets = hands(who, model)
        env = {**os.environ, **secrets}
        args = ["run", "--name", job, "--gpus", "all", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "4096", "--memory", os.getenv("JUGNU_AUDITOR_MEMORY", "8g"),
                "-v", f"{job}:/work", "-v", f"{CACHE}:/home/auditor/.cache", "-w", "/work"]
        for name in mounted:
            args += ["-v", f"{src / name}:/work/{name}:ro"]
        for name in secrets:
            args += ["-e", name]
        try:
            reply = docker(*args, "--entrypoint", "bash", IMAGE, "-c", command, timeout=minutes * 60, env=env)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", job], capture_output=True)
            reply = f"(stopped after its {minutes:.0f} minutes)"
        except RuntimeError as e:  # it ended with an error: still keep its transcript and any findings
            reply = f"(it stopped with an error: {e})"
        logs = subprocess.run(["docker", "logs", job], capture_output=True, text=True, encoding="utf-8",
                              errors="replace").stdout
        (out / f"{who} transcript.txt").write_text(logs or reply, encoding="utf-8")
        subprocess.run(["docker", "cp", f"{job}:/work/{findings(who)}", str(out)], capture_output=True)
        return reply.strip(), out
    finally:
        subprocess.run(["docker", "rm", "-f", job], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", job], capture_output=True)


def gpt(task, folder, minutes=30):
    """GPT through GPT_MODELS: a model that isn't available passes the job on; a safety decline ends it."""
    skipped = []
    for model in GPT_MODELS:
        reply, out = run("GPT", task, folder, model, minutes)
        if reply.startswith("(it stopped with an error") and UNAVAILABLE.search(reply) and not BLOCKED.search(reply):
            skipped.append(model)
            print(f"  (GPT: {model} isn't available; trying the next)", flush=True)
            continue
        if len(reply) < 800 and (DECLINED.search(reply) or BLOCKED.search(reply)):
            return f"GPT ({model}) declined: {reply[:300]} GPT sits this one out.", out
        note = f" (not available: {', '.join(skipped)})" if skipped else ""
        return f"[answered by {model}{note}]\n{reply}", out
    raise RuntimeError(f"none of GPT's models is available: {', '.join(skipped)}")


def build():
    ensure_docker()
    print(f"building {IMAGE} (about 10 GB; 10 minutes or so the first time)...", flush=True)
    failed = subprocess.run(["docker", "build", "-t", IMAGE, str(ROOT / "auditor")]).returncode
    print("\nThe build stopped (often a download timing out). Run it again: it carries on from where it stopped."
          if failed else f"\n{IMAGE} is built: the council can work hands-on now.", flush=True)
    if os.getenv("JUGNU_BUILD_WINDOW"):  # started in a window of its own: keep it open to be read
        input("Press Enter to close this window.")
    if failed:
        sys.exit(1)


def check():
    """Each member runs a one-line job in a container. Costs a few cents."""
    import tempfile
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    import council
    folder = Path(tempfile.mkdtemp(prefix="jugnu-check-"))
    (folder / "gpu_check.py").write_text("import torch\nprint(sum(range(10)), torch.cuda.is_available())\n")
    task = "Run gpu_check.py with python and reply with exactly what it printed. Write no findings file."
    models = {"claude": ("Claude", council.CLAUDE_MODEL), "gemini": ("Gemini", council.GEMINI_MODEL),
              "deepseek": ("DeepSeek", council.DEEPSEEK_MODEL),
              **{seat: (name, os.getenv(model, "")) for seat, (name, _, _, model) in council.CHAT_SEATS.items()
                 if seat in council.HANDS_ON}}
    for seat in council.MEMBERS:  # every member of your council that can work hands-on
        if seat != "gpt" and not models.get(seat, ("", ""))[1]:
            continue
        who, model = ("GPT", None) if seat == "gpt" else models[seat]
        go = (lambda: gpt(task, folder, 10)[0]) if seat == "gpt" else (lambda who=who, model=model: run(who, task, folder, model, 10)[0])
        start = time.perf_counter()
        try:
            reply = go().strip()
            print(f"{who:9} ran {time.perf_counter() - start:5.0f}s  {reply[-160:]!r}", flush=True)
        except Exception as e:
            print(f"{who:9} no  {time.perf_counter() - start:5.0f}s  {' '.join(str(e).split())[:200]}", flush=True)
    print("(right answer: 45 True)")


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
    elif sys.argv[1:] == ["window"]:  # the build in a window of its own, so whoever started it isn't kept waiting
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "build"], cwd=ROOT,
                         env={**os.environ, "JUGNU_BUILD_WINDOW": "1"},
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        print("The build has started in a window of its own: about 10 GB, 10 minutes or more the first time.")
    elif sys.argv[1:] == ["check"]:
        check()
    else:
        assert brief("GPT", "D:/x", ["models"], "Audit it.").count("/work/models") == 1
        STATUS.update(GPT={"state": "done", "since": 0, "took": 130},
                      Gemini={"state": "waiting", "since": 0})
        assert progress()[0] == "GPT finished after 2 minutes. Gemini is waiting its turn.", progress()
        STATUS["GPT"]["state"] = "failed"
        assert progress()[0].startswith("GPT failed after 2 minutes."), progress()
        STATUS.clear()
        assert "GPT findings.md" in brief("GPT", "D:/x", [], "t")
        assert VENV.match(".venv-esm2") and VENV.match(".venv") and not VENV.match("venues")
        assert LINK.match("models") and not LINK.match("core")
        assert UNAVAILABLE.search("error: model 'gpt-6.1-sol' not found") and not BLOCKED.search("model not found")
        assert BLOCKED.search("flagged for possible biological risk")
        assert DECLINED.search("I can't help with that request.")
        os.environ.setdefault("XAI_API_KEY", "k")
        command, secrets = hands("Grok", "grok-4.7")  # Grok through Codex: its own provider, its own key and nothing else
        assert "model_provider=xai" in command and "-m grok-4.7" in command and list(secrets) == ["XAI_API_KEY"], command
        print("ok  (python auditors.py build, then python auditors.py check)")
