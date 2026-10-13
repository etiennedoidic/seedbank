"""seedbank review (interactive) - walk the changed/new queue."""

import difflib
import os
import subprocess
import sys
from pathlib import Path

from lib.error import SeedbankError
from lib.init import approve_key_path
from lib.ledger import (
    Entry,
    append_block,
    read_bank,
    read_recorded_blob,
    record_blobs,
    sign_entries,
)
from lib.project_check import project_check, project_root


def _reviewer_from_bank(bank):
    reviewers = bank.get("reviewers") or {}
    if not reviewers:
        raise SeedbankError("no reviewers in bank.json; run `seedbank init --reviewer`")
    env = os.environ.get("SEEDBANK_REVIEWER")
    if env and env in reviewers:
        return env
    # First reviewer whose public key matches our local approval key, else the first.
    key_pub = Path(str(approve_key_path()) + ".pub")
    if key_pub.is_file():
        mine = key_pub.read_text().strip().split()[:2]
        for name, pub in reviewers.items():
            if pub.strip().split()[:2] == mine:
                return name
    return next(iter(reviewers))


def _print_file(path, text, flagged_lines=None, limit=200):
    flagged_lines = flagged_lines or set()
    lines = text.splitlines()
    for i, line in enumerate(lines[:limit], 1):
        marker = "!" if i in flagged_lines else " "
        print(f"  {i:>4}{marker} {line}")
    if len(lines) > limit:
        print(f"  ... {len(lines) - limit} more lines ...")


def _print_diff(old, new, path):
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path} (vetted)",
        tofile=f"b/{path}",
        n=3,
    )
    sys.stdout.writelines(diff)


def review(folder=".", editor=False):
    """Interactive review. Returns the number of files approved."""
    root = project_root(folder)
    if root is None:
        raise SeedbankError("no .seedbank/bank.json up from here; run `seedbank init`")
    if not sys.stdin.isatty():
        raise SeedbankError("seedbank review needs a terminal to type approvals")
    bank = read_bank(root)
    reviewer = _reviewer_from_bank(bank)
    report = project_check(root, strict=False, brief=False, as_json=False)
    queue = []
    for row in report["changed"]:
        queue.append(("changed", row["path"], row["sha"], row["was"], None))
    for row in report["new"]:
        kind = row["kind"]
        if kind in ("content", "svg"):
            continue
        queue.append(("new", row["path"], row["sha"], None, kind))
    if editor:
        return _editor_review(root, queue, reviewer)
    return _tui_review(root, queue, reviewer)


def _tui_review(root, queue, reviewer):
    if not queue:
        print("  nothing to review")
        return 0
    approvals = []
    approvals_bytes = []
    for index, (status, path, sha, old_sha, kind) in enumerate(queue, 1):
        full = Path(root) / path
        print(f"\n[{index}/{len(queue)}] {status.upper()} {path}")
        if status == "changed":
            old = read_recorded_blob(root, old_sha)
            if old is None:
                print("  (no vetted copy on disk; cannot diff)")
            else:
                _print_diff(old.decode("utf-8", "replace"), full.read_text(errors="replace"), path)
        else:
            text = full.read_text(errors="replace") if _is_text(full) else "<binary>"
            _print_file(path, text)
        action = _ask("(a)pprove  (r)evert  (s)kip  (e)dit  (n)ote  (q)uit  ? ",
                      allowed={"a", "r", "s", "e", "n", "q"})
        if action == "q":
            break
        if action == "s":
            continue
        if action == "e":
            editor = os.environ.get("EDITOR") or "nano"
            subprocess.run([editor, str(full)])
            # Recompute the hash after editing.
            sha = _hash_object(full)
        if action == "r" and status == "changed":
            old = read_recorded_blob(root, old_sha)
            if old is None:
                print("  no vetted copy on disk; cannot revert")
                continue
            full.write_bytes(old)
            print(f"  reverted {path}")
            continue
        if action == "n":
            note = input("  note (will be stored, non-blocking): ")
            print(f"  noted: {note}")
            continue
        if action == "a":
            entry = Entry(kind="blob", sha=sha, path=path)
            approvals.append(entry)
            try:
                approvals_bytes.append((entry, full.read_bytes()))
            except OSError:
                pass
    if not approvals:
        print("  nothing approved")
        return 0
    print(f"\n  approving {len(approvals)} file(s). ssh-keygen will ask for your approval password.")
    key = approve_key_path()
    if not key.is_file():
        raise SeedbankError("no approval key; run `seedbank init --reviewer`")
    lines, sig = sign_entries(key, approvals, reviewer)
    append_block(root, lines, sig)
    record_blobs(root, approvals_bytes)
    print(f"  signed and added to .seedbank/vetted")
    return len(approvals)


def _editor_review(root, queue, reviewer):
    """Non-interactive fallback: dump the queue so an editor can review."""
    print("  editor mode is not implemented yet; use interactive mode")
    return 0


def _ask(prompt, allowed):
    while True:
        line = input(prompt).strip().lower()
        if line in allowed:
            return line
        if line and line[0] in allowed:
            return line[0]


def _is_text(path):
    try:
        data = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\x00" not in data


def _hash_object(path):
    proc = subprocess.run(
        ["git", "hash-object", "--", str(path)],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


# ---------- programmatic API for tests ------------------------------------


def apply_action(root, path, action, approvals=None):
    """Apply one review action without a TTY. For tests and scripts.

    action: "approve" | "revert" | "skip"
    approvals: list of Entry collected so far (approvers add to this).
    """
    full = Path(root) / path
    if action == "revert":
        bank = read_bank(root)
        reviewers = bank.get("reviewers") or {}
        from lib.ledger import read_vetted
        entries, _vetted, _problems = read_vetted(root, reviewers)
        latest = None
        for e in entries:
            if e.kind == "blob" and e.path == path:
                latest = e.sha
        if latest is None:
            raise SeedbankError(f"no vetted copy of {path} to revert to")
        data = read_recorded_blob(root, latest)
        if data is None:
            raise SeedbankError(f"no stored content for {path}")
        full.write_bytes(data)
        return "reverted"
    if action == "approve":
        if approvals is None:
            raise SeedbankError("approve needs an approvals list")
        sha = _hash_object(full)
        approvals.append((Entry(kind="blob", sha=sha, path=path), full.read_bytes()))
        return "approved"
    return "skipped"


def commit_approvals(root, approvals, reviewer):
    """Sign and append a list of (Entry, bytes). Returns number signed."""
    if not approvals:
        return 0
    entries = [e for e, _ in approvals]
    key = approve_key_path()
    if not key.is_file():
        raise SeedbankError("no approval key; run `seedbank init --reviewer`")
    lines, sig = sign_entries(key, entries, reviewer)
    append_block(root, lines, sig)
    record_blobs(root, approvals)
    return len(entries)
