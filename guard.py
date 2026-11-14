"""Jugnu's safeguards, and the one way its own program may change.

No brain can edit this file, in any mode: self_block() below refuses it, whoever asks. Everything that
decides what needs your spoken OK (risk), what is refused outright (drive_block) and how a change to
Jugnu's own code reaches the running copy (prepare, check, apply, undo) lives here for that reason.
"""
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def drive_block(tool, inp):
    """Why this Google call is refused outright (no asking); None if it's allowed.
    Google has no 'edit but never delete' permission, so the line is drawn here."""
    if not tool.startswith("mcp__google__"):
        return None
    yes = lambda k: inp.get(k) in (True, "true", "True", 1)
    if re.search(r"\b(?:TRASH|SPAM)\b", json.dumps(inp.get("add_label_ids") or "")):
        return "Trashing mail or marking it as spam is off-limits. Ask the user to do it themselves."
    if yes("trashed"):
        return "Deleting or trashing anything in their Google Drive is off-limits. Ask the user to do it themselves."
    if yes("clear_values"):
        return "Clearing cells in their sheets is off-limits: it deletes the data. Ask the user to do it themselves."
    if inp.get("remove_parents") and not inp.get("add_parents"):  # out of every folder: lost, in effect
        return "Taking a file out of its folders without moving it elsewhere is off-limits."
    for key in ("action", "operation", "mode"):
        if re.search(r"delete|remove|trash|revoke|empty", str(inp.get(key, "")), re.I):
            return "Deleting or removing anything in their Google account is off-limits. Ask the user to do it themselves."
    return None


# Risky actions: it asks you first. Everything else runs without asking.
# a key file named in a path or anywhere in a command ("cat .env", "type C:\\Users\\you\\.ssh\\id_rsa")
SECRETS = re.compile(r"(^|[\\/\s\"'=])(\.env|\.ssh|id_rsa|id_ed25519)([\\/.\s\"';|&)]|$)"
                     r"|(^|[\\/])credentials([\\/.]|$)|\w\.(pem|key|pfx)([\s\"';|&]|$)", re.I)
WRITTEN = ("content", "new_string", "old_string")  # what goes into a file: text, not a path or a command
RISKY_COMMANDS = [
    (r"\b(rm|rmdir|del|erase|rd|shred|unlink)\b|Remove-Item|\bri\b", "deletes files"),
    (r"\bfind\b.*\s-delete\b|shutil\.rmtree|os\.(remove|unlink|rmdir)\(|\.unlink\(|robocopy\b.*/(MIR|PURGE)\b",
     "deletes files"),
    (r"\b(truncate|Clear-Content)\b|\bdd\b.*\bof=", "empties or overwrites a file"),
    (r"\bprintenv\b|(^|[;&|]\s*)env\s*($|[|>;&])|\b(gci|dir|ls|Get-ChildItem)\s+env:|\$env:\w*(KEY|TOKEN|SECRET)"
     r"|\$\{?\w*(API_?KEY|_TOKEN|_SECRET)", "reads your saved keys"),
    (r"\bgit\s+(push\s+.*(-f|--force)|reset\s+--hard|clean|checkout\s+(--|\.(\s|$))|restore|branch\s+-D|rebase|"
     r"stash\s+(drop|clear))", "rewrites git history"),
    (r"(curl|wget|iwr|Invoke-WebRequest|irm)\b.*\|\s*(ba)?sh|\biex\b|Invoke-Expression",
     "downloads and runs code"),
    (r"\bformat\s+[a-z]:|\b(mkfs|diskpart|shutdown|reboot|bcdedit|reg\s+(add|delete)|Set-ExecutionPolicy|"
     r"sudo|chmod|chown|icacls|takeown)\b", "changes system settings"),
    (r"\b(taskkill|kill|pkill|Stop-Process)\b", "stops running programs"),
    (r"\b(pip|npm|winget|scoop|choco|uv)\s+(uninstall|remove)\b", "uninstalls software"),
    (r"\b(mv|move)\b", "moves files, which can overwrite others"),
]
REDIRECT = re.compile(r"(?<![>2&])>(?!>|&)\s*(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")


def _overwrites(command):
    """True if a `>` in this command would truncate a file that already exists.
    Creating a new file is harmless, so only an existing target counts."""
    base = ROOT
    at = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)", command)
    if at:
        base = Path(_real(at.group(1)))
    for target in REDIRECT.findall(command):
        target = _real(target)
        if target.lower() in ("/dev/null", "nul"):
            continue
        path = Path(target)
        try:
            if (path if path.is_absolute() else base / path).exists():
                return True
        except OSError:
            pass
    return False


def _real(token):
    """Strip quotes, and turn Git Bash's /c/Users/... into C:\\Users\\..."""
    token = token.strip("\"'")
    return token[1] + ":" + token[2:] if re.match(r"^/[a-zA-Z]/", token) else token
IRREVERSIBLE = re.compile(r"\b(send|submit|post|publish|pay|buy|purchase|order|checkout|delete|"
                          r"remove|confirm|transfer|sign|accept|unsubscribe)\b", re.I)


def risk(tool, inp):
    """Why this action needs your OK, in a few spoken words; None if it doesn't."""
    blob = json.dumps(inp)
    if any(SECRETS.search(v) for k, v in inp.items() if isinstance(v, str) and k not in WRITTEN):
        return "touches a password or key file"
    if tool == "Bash":
        for pattern, why in RISKY_COMMANDS:
            if re.search(pattern, inp.get("command", ""), re.I):
                return why
        if _overwrites(inp.get("command", "")):
            return "overwrites an existing file"
    if tool == "Write" and os.path.exists(inp.get("file_path", "")):
        return "overwrites an existing file"
    if tool == "mcp__google__update_drive_file" and any(inp.get(k) for k in ("content", "file_path", "file_url")) \
            and inp.get("mode", "replace") == "replace":
        return "replaces a file's contents in your Google Drive"
    if tool == "mcp__google__format_sheet_range" and inp.get("merge_cells") in (True, "true"):
        return "merges cells, which keeps only the top-left value"
    if tool.startswith("mcp__browser__") and tool.split("__")[-1] in (
            "browser_click", "browser_press_key", "browser_fill_form", "browser_select_option",
            "browser_file_upload", "browser_evaluate", "browser_run_code_unsafe"):
        if tool.endswith(("evaluate", "run_code_unsafe")) or IRREVERSIBLE.search(blob):
            return "could send or submit something"
    return None


# ---- its own program ----
# Self-improve mode (the tray switches it; off unless you turn it on). Off: no brain may change Jugnu's
# program at all. On: it may work on a copy (_next/), never on the running files; the copy replaces them
# only through apply(), after check() passes and you say so, and the files it replaced are kept for undo().
STATE = ROOT / "logs" / "self_improve.json"
NEXT, PREVIOUS = ROOT / "_next", ROOT / "_previous"
NOTES = {"heard.txt", "pronunciations.txt", "colors.txt", "personality.md", "about_me.md"}  # its notes: always its to edit
_WRITES = re.compile(r">|\b(set-content|add-content|out-file|copy-item|move-item|remove-item|rename-item|cp|copy|mv|move|"
                     r"del|rm|ren|rename|tee)\b|sed\s+-i|open\(|write_text|write_bytes|\.write\(", re.I)


def program(root=None):
    """The files that are Jugnu's program, as paths relative to its folder."""
    root = root or ROOT
    found = [p.name for p in root.iterdir() if p.is_file() and (
        p.suffix in (".py", ".bat", ".ps1") or p.name in ("requirements.txt", "requirements-local.txt",
                                                           "gpu-overrides.txt", ".env.example", "README.md"))]
    return sorted(found + [x for x in ("face/face.html", "setup/setup.html", "auditor/Dockerfile") if (root / x).exists()])


def state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember(**changes):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({**state(), **changes}), encoding="utf-8")


def self_block(tool, inp, improving=False):
    """Why this may not touch Jugnu's own program; None if it may. This file: never. The rest of the program:
    only the copy in _next/, and only while self-improve mode is on. Its notes (NOTES) are always its to edit."""
    root = str(ROOT).replace("\\", "/").lower()
    names = [n.lower() for n in program()] + ["guard.py"]
    if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "").replace("\\", "/").lower()
        absolute = bool(re.match(r"([a-z]:)?/", path))
        if absolute and not path.startswith(root + "/"):
            return None
        rel = (path[len(root) + 1:] if absolute else path).lstrip("./")
        parts = rel.split("/")
        if parts[-1] == "guard.py":
            return "Its own safeguards are off-limits, whoever asks: tell the user so."
        if parts[0] == "_previous":
            return "Those are the kept copies for undoing a change: off-limits."
        if parts[0] == "_next":
            return None if improving else ("Self-improve mode is off, so its own program can't be changed, not even "
                                           "on a copy. The user can turn it on from the tray.")
        if rel in names:
            return ("Its own running program is off-limits. With self-improve mode on, make the change to the copy "
                    "of the same file in _next/ instead; the user applies it by saying \"apply the change\"."
                    if improving else
                    "Self-improve mode is off, so its own program can't be changed. The user can turn it on from the tray.")
        return None
    if tool == "Bash":
        command = str(inp.get("command") or "")
        # output sent nowhere, or one stream into another, writes no file ("2>&1", "> $null")
        command = re.sub(r"\d?>&\d|\d?>\s*(?:nul|\$null|/dev/null)\b", " ", command, flags=re.I)
        if not _WRITES.search(command):
            return None
        low = command.replace("\\", "/").lower()
        if "guard.py" in low:
            return "Its own safeguards are off-limits, whoever asks: tell the user so."
        if "_previous" in low:
            return "Those are the kept copies for undoing a change: off-limits."
        # a program file that is only being run isn't being written ("python auditors.py build > build.log": seen in use)
        low = re.sub(r"\bpython[\w.]*\"?\s+(?:\"[^\"]*/|[^\s\"|;&>]*/)?\"?(?:" + "|".join(map(re.escape, names)) + r")\b\"?",
                     " python (its own program, run) ", low)
        for name in names:
            for m in re.finditer(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", low):
                before = low[max(0, m.start() - 6):m.start()]
                if not (improving and before.endswith("_next/")) and not (improving and "_next" in low.split(name)[0][-40:]):
                    return ("Its own running program is off-limits to commands that write. With self-improve mode on, "
                            "work in _next/ instead." if improving else
                            "Self-improve mode is off, so its own program can't be changed.")
    return None


def _sum(path):
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def prepare():
    """A fresh working copy of the program in _next/, for a brain to change, and a note of what it was copied from."""
    for rel in program() + ["guard.py"]:
        (NEXT / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, NEXT / rel)
    (NEXT / "base.json").write_text(json.dumps({rel: _sum(ROOT / rel) for rel in program()}), encoding="utf-8")
    return NEXT


def _base():
    try:
        return json.loads((NEXT / "base.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def changed():
    """Program files a brain has changed in _next/ since the copy was made (guard.py never counts)."""
    base = _base()
    return [rel for rel in program(NEXT) if rel != "guard.py" and _sum(NEXT / rel) != base.get(rel, "")] if base else []


def check():
    """Run the copy's own checks, the way run_checks.bat does. (True, "") or (False, what failed)."""
    if not filecmp.cmp(NEXT / "guard.py", ROOT / "guard.py", shallow=False):
        return False, "the safeguards in the copy have been altered"
    runs = [[m + ".py"] for m in ("guard", "pronounce", "speak", "ears", "face", "argon", "auditors")]
    runs += [["setup_jugnu.py", "test"], ["-c", "import jugnu; jugnu._selfcheck()"]]
    for args in runs:
        try:
            done = subprocess.run([sys.executable, *args], cwd=NEXT, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return False, f"{args[0]} did not finish"
        if done.returncode:
            return False, f"{args[0]}: " + (done.stderr or done.stdout).strip().splitlines()[-1][:200]
    return True, ""


def apply():
    """Swap the changed files in, keeping the ones they replace. Returns their names."""
    files, base = changed(), _base()
    moved = [rel for rel in files if _sum(ROOT / rel) != base.get(rel, "")]
    if moved:  # updated by you or an update since the copy was made: swapping in would undo that
        raise ValueError("the program has changed since the copy was made: " + ", ".join(moved))
    if files:
        kept = PREVIOUS / time.strftime("%Y-%m-%d %H%M%S")
        for rel in files:
            (kept / rel).parent.mkdir(parents=True, exist_ok=True)
            if (ROOT / rel).exists():
                shutil.copy2(ROOT / rel, kept / rel)
            (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(NEXT / rel, ROOT / rel)
        remember(last=kept.name)
        prepare()  # the copy starts again from what is now running
    return files


def undo():
    """Put back the files the last apply() replaced. Returns their names ([] if there is nothing to undo)."""
    last = state().get("last")
    kept = PREVIOUS / last if last else None
    if not kept or not kept.exists():
        return []
    files = [str(p.relative_to(kept)).replace("\\", "/") for p in kept.rglob("*") if p.is_file()]
    for rel in files:
        if rel != "guard.py":
            shutil.copy2(kept / rel, ROOT / rel)
    remember(last=None)
    return files


if __name__ == "__main__":
    import tempfile
    assert risk("Read", {"file_path": r"C:\\Users\\you\\.ssh\\id_rsa"}) and risk("Edit", {"file_path": "notes.md", "old_string": "a", "new_string": "b"}) is None
    here = str(ROOT)
    off = lambda tool, **inp: self_block(tool, inp, False)
    on = lambda tool, **inp: self_block(tool, inp, True)
    assert off("Edit", file_path=f"{here}/ears.py") and on("Edit", file_path=f"{here}/ears.py")  # the running program: never directly
    assert off("Edit", file_path="_next/ears.py") and on("Edit", file_path="_next/ears.py") is None  # the copy: only with the mode on
    assert on("Edit", file_path=f"{here}/_next/guard.py") and on("Write", file_path="guard.py") and off("Write", file_path="GUARD.PY")
    assert on("Write", file_path=f"{here}/_previous/x/ears.py")
    assert off("Edit", file_path="heard.txt") is None and off("Write", file_path=f"{here}/pronunciations.txt") is None
    assert off("Write", file_path="C:/Users/you/Documents/report.md") is None and off("Write", file_path=f"{here}/logs/x.md") is None
    assert off("Bash", command="python -c \"open('ears.py','w').write('x')\"") and on("Bash", command="Set-Content guard.py 'x'")
    assert off("Bash", command="Get-Content ears.py") is None and on("Bash", command="cd _next; ..\\.venv\\Scripts\\python.exe ears.py") is None
    assert on("Bash", command="Set-Content _next/ears.py 'x'") is None and off("Bash", command="Set-Content _next/ears.py 'x'")
    assert off("Bash", command="echo hi > notes.txt") is None
    # running its own program isn't changing it, whatever is done with the output (seen in use)
    assert off("Bash", command="python auditors.py build 2>&1") is None
    assert off("Bash", command=".venv\\Scripts\\python.exe auditors.py build > build.log") is None
    assert off("Bash", command='& "D:\\Some folder\\x\\.venv\\Scripts\\python.exe" "D:\\Some folder\\x\\auditors.py" build 2>&1 | Tee-Object build.log') is None
    assert off("Bash", command="python auditors.py build > ears.py") and off("Bash", command="python ears.py; Set-Content ears.py 'x'")
    assert off("Bash", command="python guard.py > out.txt") and off("Bash", command="python -c \"open('ears.py','w')\" 2>&1")
    # prepare -> change -> apply -> undo, in a scratch folder
    real = ROOT, NEXT, PREVIOUS, STATE
    with tempfile.TemporaryDirectory() as tmp:
        ROOT = Path(tmp); NEXT, PREVIOUS, STATE = ROOT / "_next", ROOT / "_previous", ROOT / "logs" / "self_improve.json"
        for name, text in (("ears.py", "old"), ("guard.py", "g"), ("jugnu.py", "same")):
            (ROOT / name).write_text(text)
        prepare()
        assert changed() == [] and apply() == []
        (NEXT / "ears.py").write_text("new"); (NEXT / "guard.py").write_text("tampered")
        assert changed() == ["ears.py"] and check()[0] is False  # altered safeguards fail the check
        assert apply() == ["ears.py"] and (ROOT / "ears.py").read_text() == "new" and (ROOT / "guard.py").read_text() == "g"
        assert undo() == ["ears.py"] and (ROOT / "ears.py").read_text() == "old" and undo() == []
        prepare(); (NEXT / "ears.py").write_text("newer"); (ROOT / "ears.py").write_text("updated by hand")
        try:
            apply(); raise AssertionError("should refuse: the running file moved on")
        except ValueError:
            assert (ROOT / "ears.py").read_text() == "updated by hand"
    ROOT, NEXT, PREVIOUS, STATE = real
    print("ok")
