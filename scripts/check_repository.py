"""Catch common accidental inclusions; this is not a comprehensive secret scanner."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "venv", "dist", "build", "__pycache__", "_local",
        ".pytest_cache", ".mypy_cache", ".ruff_cache"}
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+(?:/|\b)")
TOKEN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
STATE_NAMES = {"models.json", "auth.json", "credentials.json", ".env"}
STATE_DIRS = {"state", "sessions", "logs", ".model-locks"}


def main():
    problems = []
    checked = 0
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP for part in relative.parts):
            continue
        if path.is_symlink():
            problems.append(f"{relative}: symbolic link needs explicit release review")
            continue
        if not path.is_file():
            continue
        checked += 1
        if (path.name in STATE_NAMES or path.name.startswith(".env.")
                or path.suffix in {".db", ".sqlite", ".sqlite3", ".jsonl", ".log", ".lock"}
                or path.name.endswith((".db-wal", ".db-shm"))
                or any(part in STATE_DIRS for part in relative.parts[:-1])):
            problems.append(f"{relative}: possible runtime data or private configuration")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{relative}: binary file needs explicit release review")
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for label, pattern in (("personal home path", PRIVATE_PATH),
                                   ("possible credential", TOKEN),
                                   ("private key", PRIVATE_KEY)):
                if pattern.search(line):
                    # Do not print the matched value, which might itself be a secret.
                    problems.append(f"{relative}:{number}: {label}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"Checked {checked} files: no flagged paths, credentials, or runtime data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
