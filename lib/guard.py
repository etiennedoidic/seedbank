"""seedbank guard <path> - exit 0 if not vetted, 2 if it is."""

import subprocess
import sys
from pathlib import Path

from lib.error import SeedbankError
from lib.ledger import read_bank, read_vetted
from lib.project_check import project_root


def _hash_object(path):
    proc = subprocess.run(
        ["git", "hash-object", "--", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def guard(path_arg):
    path = Path(path_arg).resolve()
    root = project_root(path.parent if path.exists() else Path.cwd())
    if root is None:
        return 0
    bank = read_bank(root)
    reviewers = bank.get("reviewers") or {}
    entries, _vetted, _problems = read_vetted(root, reviewers)
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return 0
    vetted_blob = None
    for entry in entries:
        if entry.kind == "blob" and entry.path == rel:
            vetted_blob = entry.sha
    if vetted_blob is None:
        return 0
    if not path.is_file():
        print(f"this file is vetted; editing it means it has to be reviewed again: {rel}",
              file=sys.stderr)
        return 2
    sha = _hash_object(path)
    if sha != vetted_blob:
        # Already different; already flagged by check. Still block the edit.
        print(f"{rel} is vetted and has been changed; approve or revert in `seedbank review`",
              file=sys.stderr)
        return 2
    print(f"this file is vetted; editing it means it has to be reviewed again: {rel}",
          file=sys.stderr)
    return 2
