"""Rewrite terms into how people actually say them, just before speaking."""
import re
from pathlib import Path

# Yours, not the project's: it holds the terms and names you use, so it's made on your computer (by setup,
# by Jugnu when you correct it, or by hand) and .gitignore keeps it out of anything you publish.
FILE = Path(__file__).resolve().parent / "pronunciations.txt"
HEAD = """# How Jugnu should say terms out loud. One per line:  TERM = how to say it
# Only the voice sees these; what you read on screen stays unchanged.
# Matching is case-sensitive and whole-word.
# The ears use the same list: they are told to expect these terms, so they
# write SQL, not "sequel". A term spoken fine as-is can map to itself (SQL = SQL).
"""


def load(path=FILE):
    rules = {}
    if not path.exists():  # nothing taught yet
        return rules
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            term, said = (s.strip() for s in line.split("=", 1))
            rules[term] = said
    return rules


def speakable(text, rules=None):
    rules = load() if rules is None else rules
    for term in sorted(rules, key=len, reverse=True):  # longest first: "Wi-Fi" before "Wi"
        text = re.sub(rf"(?<![\w-]){re.escape(term)}(?![\w-])", rules[term], text)
    return text


if __name__ == "__main__":
    r = {"Wi-Fi": "why fie", "SQL": "sequel"}
    assert speakable("Wi-Fi hands off to SQL.", r) == "why fie hands off to sequel."
    assert speakable("Wi-Fi-like networks", r) == "Wi-Fi-like networks"  # left alone, needs its own rule
    assert speakable("SQLX and pSQL stay", r) == "SQLX and pSQL stay"
    assert load(FILE.with_name("no-such-file.txt")) == {}
    print("ok")
