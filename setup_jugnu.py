"""Setup: a window that walks through Jugnu's brain, council, voice and ears, and extras, checks
each key as you go, and writes .env. Nothing here needs a terminal except a sign-in, which it opens.

    python setup_jugnu.py        (install.bat runs it after installing; run it again any time)

Everything it does is also a line in .env, documented in .env.example.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pronounce  # the words it should know live in pronunciations.txt

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
PAGE = ROOT / "setup" / "setup.html"
PERSONALITY = ROOT / "personality.md"
WINDOWS = sys.platform == "win32"

# what each provider's key is called in .env (the council shares them)
KEYS = {"claude-subscription": "CLAUDE_CODE_OAUTH_TOKEN", "claude-api": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
        "xai": "XAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "custom": "JUGNU_BRAIN_KEY"}
BASES = {"openai": "https://api.openai.com/v1", "deepseek": "https://api.deepseek.com", "xai": "https://api.x.ai/v1",
         "openrouter": "https://openrouter.ai/api/v1", "ollama": "http://localhost:11434/v1"}
SECRET = re.compile(r"KEY|TOKEN|SECRET")


def read_env():
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def _quoted(value):
    return f'"{value}"' if re.search(r"[\s#\"']", value) else value


def write_env(values):
    """Set these in .env, in place: a key's live line, else its commented example, else appended.
    Empty values are commented out, so the default applies."""
    lines = (ENV if ENV.exists() else ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for key, value in values.items():
        value = "" if value is None else str(value).strip()
        live = [i for i, l in enumerate(lines) if re.match(rf"\s*{key}\s*=", l)]
        example = [i for i, l in enumerate(lines) if re.match(rf"\s*#\s*{key}\s*=", l)]
        line = f"{key}={_quoted(value)}" if value else "# " + lines[live[0]].lstrip() if live else ""
        if live:  # emptied: commented out with its value kept, so the default applies and nothing is lost
            lines[live[0]] = line
            for i in reversed(live[1:]):  # duplicates would fight each other
                del lines[i]
        elif value and example:
            lines[example[0]] = line
        elif value:
            lines.append(line)
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Jugnu setup", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError({401: "That key wasn't accepted.", 403: "That key isn't allowed to do this."}.get(
            e.code, f"The provider said {e.code}: {e.read().decode(errors='replace')[:200]}")) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach it: {e.reason}") from None


def windows_theme():
    """The theme picked in setup (JUGNU_THEME), else Windows' own light/dark setting for apps, else None
    (another system: the page asks the browser)."""
    if read_env().get("JUGNU_THEME") in ("light", "dark"):
        return read_env()["JUGNU_THEME"]
    if not WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return None


def codex():
    """How to run Codex here: on PATH, else inside WSL (Windows), else None."""
    if shutil.which("codex"):
        return [shutil.which("codex")]
    if WINDOWS and shutil.which("wsl.exe"):
        return ["wsl.exe", "-e", "bash", "-lc", "codex"]
    return None


def codex_args(*args):
    """Codex with these arguments; inside WSL they join the bash line."""
    cmd = codex()
    return cmd[:-1] + [" ".join([cmd[-1], *args])] if cmd[0] == "wsl.exe" else cmd + list(args)


def claude_cli():
    """The Claude Code Jugnu runs on: the copy inside the Agent SDK (installed with Jugnu), else one on PATH."""
    try:
        import claude_agent_sdk
        exe = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if WINDOWS else "claude")
        if exe.exists():
            return str(exe)
    except ImportError:
        pass
    return shutil.which("claude")


def terminal(command):
    """Open a console window running this, for sign-ins that need a browser and a person."""
    if WINDOWS:
        subprocess.Popen(["cmd", "/c", "start", "Jugnu setup", "cmd", "/k", command])
    elif sys.platform == "darwin":
        subprocess.Popen(["osascript", "-e", f'tell app "Terminal" to do script "{command}"'])
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", f"bash -c '{command}; exec bash'"])


MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
STARTUP = MENU / "Startup"


def shortcut(link):
    """A Windows shortcut at `link` that starts Jugnu, with its icon (a .bat can't have an icon of its own).
    Paths go in through the environment, so spaces and quotes in them can't break the command."""
    link.parent.mkdir(parents=True, exist_ok=True)
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:HL_LINK); $s.TargetPath = $env:HL_TARGET; "
          "$s.WorkingDirectory = $env:HL_DIR; $s.IconLocation = $env:HL_ICON + ',0'; $s.WindowStyle = 7; "
          "$s.Description = 'Jugnu, your voice assistant'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, env={
        **os.environ, "HL_LINK": str(link), "HL_TARGET": str(ROOT / "start_jugnu.bat"), "HL_DIR": str(ROOT),
        "HL_ICON": str(ROOT / "jugnu.ico")})


def shortcuts():
    """Jugnu, with its icon: in this folder and in the Start menu (so Windows search finds it)."""
    if WINDOWS:
        shortcut(ROOT / "Jugnu.lnk")
        shortcut(MENU / "Jugnu.lnk")


class Api:
    """What the page calls (window.pywebview.api.*). Each returns plain JSON."""

    window = None

    def info(self):
        env = read_env()
        gpus = []
        if shutil.which("nvidia-smi"):
            try:
                r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
                for row in r.stdout.strip().splitlines():
                    name, total, free = [x.strip() for x in row.split(",")]
                    gpus.append({"name": name, "total": int(total), "free": int(free)})
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return {"env": {k: ("set" if SECRET.search(k) and v else v) for k, v in env.items()},
                "gpus": gpus, "windows": WINDOWS, "theme": windows_theme(),
                "personality": PERSONALITY.read_text(encoding="utf-8").strip() if PERSONALITY.exists() else "",
                "words": list(pronounce.load()),
                "at_login": (STARTUP / "Jugnu.lnk").exists(),  # the box on the last screen shows what's so now
                "has": {"codex": bool(codex()), "claude": bool(claude_cli()), "node": bool(shutil.which("npx")),
                        "gcloud": bool(shutil.which("gcloud")), "uvx": bool(shutil.which("uvx"))}}

    def models(self, provider, auth, key, url):
        """Check a brain's key; the models it offers, newest names first."""
        env = read_env()
        key = key or env.get(KEYS.get(f"claude-{auth}" if provider == "claude" else provider, ""), "")
        try:
            if provider == "claude":
                if auth == "subscription":
                    return self.claude_status()
                data = _get("https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"})
                return {"ok": True, "models": [m["id"] for m in data.get("data", [])]}
            if provider == "openai" and auth == "subscription":
                return self.codex_status()
            if provider == "gemini":
                data = _get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                            {"x-goog-api-key": key})
                names = [m["name"].removeprefix("models/") for m in data.get("models", [])
                         if "generateContent" in m.get("supportedGenerationMethods", [])]
                return {"ok": True, "models": sorted(names, reverse=True)}
            base = (url or BASES.get(provider, "")).rstrip("/")
            if not base:
                return {"ok": False, "error": "Give the server's address."}
            data = _get(base + "/models", {"Authorization": f"Bearer {key}"} if key else {})
            return {"ok": True, "models": sorted((m["id"] for m in data.get("data", [])), reverse=True)}
        except Exception as e:  # noqa: BLE001 - shown on the page
            return {"ok": False, "error": str(e)}

    def codex_status(self):
        cmd = codex()
        if not cmd:
            return {"ok": False, "error": "Codex isn't installed. Install Node.js, then run: npm install -g @openai/codex",
                    "missing": True}
        try:
            r = subprocess.run(codex_args("login", "status"), capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"Couldn't run Codex: {e}"}
        text = (r.stdout + r.stderr).strip()
        signed_in = r.returncode == 0 and "not logged in" not in text.lower()
        return {"ok": signed_in, "models": [], "error": None if signed_in else "Codex isn't signed in yet."}

    def codex_login(self):
        cmd = codex()
        terminal("wsl.exe -e bash -lc \"codex login\"" if cmd and cmd[0] == "wsl.exe" else "codex login")
        return True

    def claude_status(self):
        """Signed in to Claude Code with a Claude plan? Jugnu runs on that sign-in; it never sees a token."""
        cli = claude_cli()
        if not cli:
            return {"ok": False, "error": "Claude Code is missing: run install.bat again."}
        try:
            r = subprocess.run([cli, "auth", "status", "--json"], capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
            signed_in = bool(json.loads(r.stdout or "{}").get("loggedIn"))
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"Couldn't ask Claude Code: {e}"}
        return {"ok": signed_in, "models": [], "error": None if signed_in else "Not signed in yet."}

    def claude_login(self):
        """Claude Code's own sign-in (Anthropic's browser flow) in a window of its own: it opens your browser.
        Runs the Claude Code inside the Agent SDK, which needn't be on PATH (a bare `claude` often isn't)."""
        cli = claude_cli()
        if not cli:
            return False
        if WINDOWS:
            subprocess.Popen([cli, "auth", "login"], creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            terminal(f"{shlex.quote(cli)} auth login")
        return True

    def google_signin(self):
        if not shutil.which("gcloud"):
            return {"ok": False, "error": "The Google Cloud CLI isn't installed: cloud.google.com/sdk/docs/install"}
        terminal("gcloud auth application-default login")
        return {"ok": True}

    def test_voice(self, key, voice):
        """Say one line with Google's voice, out loud, to check the key."""
        try:
            import sounddevice
            import speak
            wav = speak.Google(key or read_env().get("GOOGLE_TTS_API_KEY", ""), voice or speak.GOOGLE_VOICE)(
                "Hello. This is how I'll sound.")
            sounddevice.play(wav, speak.RATE)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}

    def test_ears(self, project, language):
        """Connect to Google Speech-to-Text the way Jugnu will."""
        os.environ["JUGNU_GOOGLE_PROJECT"] = project
        if language:
            os.environ["JUGNU_STT_LANGUAGE"] = language
        try:
            import ears
            note = ears.Google(["Jugnu"]).check()
            return {"ok": True, "note": note or ""}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e).splitlines()[0][:300]}

    def mics(self):
        try:
            import sounddevice
            names = []
            for d in sounddevice.query_devices():
                if d["max_input_channels"] > 0 and d["name"] not in names:
                    names.append(d["name"])
            return names
        except Exception:  # noqa: BLE001
            return []

    def install_local(self):
        """Install Chatterbox and Whisper (a few GB): progress lines go to the page as they come."""
        uv = shutil.which("uv")
        cmd = ([uv, "pip", "install", "--python", sys.executable] if uv else [sys.executable, "-m", "pip", "install"]) \
            + ["-r", str(ROOT / "requirements-local.txt")] \
            + (["--torch-backend", "cu128", "--override", str(ROOT / "gpu-overrides.txt")] if uv else [])

        def run():
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", cwd=ROOT)
            for line in p.stdout:
                self._js("progress", line.rstrip()[-160:])
            self._js("installed", p.wait() == 0)
        threading.Thread(target=run, daemon=True).start()
        return True

    def _js(self, fn, value):
        if self.window:
            self.window.evaluate_js(f"window.{fn} && window.{fn}({json.dumps(value)})")

    def open(self, target):
        """A web page or one of Jugnu's files, in the usual app."""
        path = ROOT / target if not target.startswith("http") else None
        if WINDOWS:
            os.startfile(str(path) if path else target)  # noqa: S606 - opening, not running, a document
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path) if path else target])
        return True

    def set_personality(self, text):
        """How Jugnu talks to you, in your words (personality.md); empty = its usual manner."""
        if text.strip():
            PERSONALITY.write_text(text.strip() + "\n", encoding="utf-8")
        elif PERSONALITY.exists():
            PERSONALITY.unlink()
        return True

    def set_words(self, text):
        """Terms you'll say often, one per line (pronunciations.txt): the ears expect them and learning mode
        practises them. One it was already taught to say keeps how it's said; one you took out goes."""
        old = pronounce.load()
        words = dict.fromkeys(w.strip() for w in text.splitlines() if w.strip() and "=" not in w)
        pronounce.FILE.write_text(pronounce.HEAD + "\n" + "".join(f"{w} = {old.get(w, w)}\n" for w in words),
                                  encoding="utf-8")
        return True

    def save(self, values):
        write_env(values)
        return True

    def start(self, at_login):
        """Start Jugnu in the tray; optionally at every login too."""
        bat = ROOT / "start_jugnu.bat"
        if WINDOWS:
            if at_login:
                shortcut(STARTUP / "Jugnu.lnk")
                old = STARTUP / "Jugnu.bat"  # what earlier setups made: replaced by the shortcut, if it was ours
                if old.exists() and str(ROOT).lower() in old.read_text(encoding="utf-8", errors="replace").lower():
                    old.unlink()
            else:  # unticked: it no longer starts with Windows
                (STARTUP / "Jugnu.lnk").unlink(missing_ok=True)
            subprocess.Popen(["cmd", "/c", str(bat)], cwd=ROOT)
        else:
            subprocess.Popen([sys.executable, str(ROOT / "tray.py")], cwd=ROOT)
        if self.window:
            threading.Timer(1.0, self.window.destroy).start()
        return True


def main():
    if sys.stderr is None:  # started without a console (pythonw): keep errors in a file instead of losing them
        (ROOT / "logs").mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(ROOT / "logs" / "setup.log", "a", encoding="utf-8", buffering=1)
        print(f"--- setup started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    import faulthandler
    faulthandler.enable()  # a hard crash (in Qt, say) still leaves where it happened in the log
    import webview
    api = Api()
    dark = windows_theme() == "dark"  # the window's own colour before the page paints: no flash
    api.window = webview.create_window("Set up Jugnu", str(PAGE), js_api=api, width=760, height=760,
                                       min_size=(520, 560), background_color="#08090a" if dark else "#f5f5f5")
    webview.start(http_server=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:  # the .env writer, on a scratch copy
        import tempfile
        ENV = Path(tempfile.mkdtemp()) / ".env"
        ENV.write_text("# JUGNU_BRAIN=claude\nGEMINI_API_KEY=old\nGEMINI_API_KEY=dup\n# keep me\n", encoding="utf-8")
        write_env({"JUGNU_BRAIN": "openai", "GEMINI_API_KEY": "new", "JUGNU_NO_SHARE": "my day job",
                   "JUGNU_USER": ""})
        text = ENV.read_text(encoding="utf-8")
        assert text.splitlines()[0] == "JUGNU_BRAIN=openai", text
        assert text.count("GEMINI_API_KEY") == 1 and "GEMINI_API_KEY=new" in text, text
        assert 'JUGNU_NO_SHARE="my day job"' in text and "# keep me" in text and "JUGNU_USER" not in text, text
        assert read_env()["JUGNU_NO_SHARE"] == "my day job"
        write_env({"JUGNU_BRAIN": ""})  # cleared: commented out, so the default (Claude) applies
        assert "# JUGNU_BRAIN=openai" in ENV.read_text(encoding="utf-8") and "JUGNU_BRAIN" not in read_env()
        write_env({"JUGNU_BRAIN": "gemini"})  # set again: the commented line comes back
        assert ENV.read_text(encoding="utf-8").count("JUGNU_BRAIN") == 1 and read_env()["JUGNU_BRAIN"] == "gemini"
        assert Api().info()["env"]["GEMINI_API_KEY"] == "set"  # keys never go back to the page
        codex = lambda: ["wsl.exe", "-e", "bash", "-lc", "codex"]
        assert codex_args("login", "status")[-1] == "codex login status"
        print("ok")
    elif sys.argv[1:] == ["shortcuts"]:  # install.bat runs this
        shortcuts()
    else:
        main()
