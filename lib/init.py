"""seedbank init - set up a project ledger."""

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from lib.error import SeedbankError
from lib.ledger import (
    Entry,
    append_block,
    bank_path,
    ledger_path,
    record_blobs,
    sign_entries,
)
from lib.review import WorkingTree, _list_working

DEFAULT_BANK = {
    "trust": ["github.com/etiennedoidic/seedbank"],
    "standards": [],
    "reviewers": {},
    "skip": ["node_modules/", ".venv/", "dist/", "build/"],
}


def approve_key_path():
    override = os.environ.get("SEEDBANK_APPROVE_KEY")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "seedbank" / "approve_key"


def _ensure_bank(root, standards=None):
    path = bank_path(root)
    if path.is_file():
        return json.loads(path.read_text())
    data = {**DEFAULT_BANK}
    data["standards"] = list(standards or [])
    data["reviewers"] = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def _write_bank(root, data):
    bank_path(root).write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def _reviewer_login():
    """Pick a stable name for the local reviewer."""
    return (
        os.environ.get("SEEDBANK_REVIEWER")
        or os.environ.get("USER")
        or getpass.getuser()
        or "reviewer"
    )


def _public_half(key_path):
    """Return the public half of an SSH key (`ssh-ed25519 AAAA... comment`)."""
    pub = Path(str(key_path) + ".pub")
    if not pub.is_file():
        raise SeedbankError(f"no public key at {pub}")
    return pub.read_text().strip()


def _test_passphrase(key_path):
    """ssh-keygen -y with an empty passphrase must fail: refuse an unlocked key."""
    if os.environ.get("SEEDBANK_ALLOW_INSECURE_KEY") == "1":
        return
    proc = subprocess.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(key_path)],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        raise SeedbankError(
            "approval key has no passphrase; remove it and run `seedbank init --reviewer` again"
        )


def create_reviewer_key(reviewer=None):
    """Create ~/.config/seedbank/approve_key, prompting for a passphrase.

    Returns (reviewer_name, public_key_text).
    """
    reviewer = reviewer or _reviewer_login()
    key = approve_key_path()
    key.parent.mkdir(parents=True, exist_ok=True)
    if key.is_file():
        _test_passphrase(key)
        return reviewer, _public_half(key)
    if not sys.stdin.isatty():
        raise SeedbankError("seedbank init --reviewer needs a terminal to type the password")
    print(f"  making your approval key at {key}")
    proc = subprocess.run(
        [
            "ssh-keygen", "-q",
            "-t", "ed25519",
            "-f", str(key),
            "-C", f"seedbank-approve ({reviewer})",
        ],
    )
    if proc.returncode != 0:
        raise SeedbankError("ssh-keygen failed to make the approval key")
    _test_passphrase(key)
    return reviewer, _public_half(key)


def baseline(root, reviewer):
    """Vet every file as-is, signed in one block."""
    _root_tree, files, trees = _list_working(root)
    key = approve_key_path()
    if not key.is_file():
        raise SeedbankError("no approval key; run `seedbank init --reviewer` first")
    entries = []
    with_bytes = []
    seen = set()
    for path, sha, _size, _mode in files:
        if path.startswith(".seedbank/"):
            continue
        if (sha, path) in seen:
            continue
        seen.add((sha, path))
        entry = Entry(kind="blob", sha=sha, path=path)
        entries.append(entry)
        try:
            data = (Path(root) / path).read_bytes()
        except OSError:
            data = b""
        with_bytes.append((entry, data))
    for path, sha in trees:
        if not path or path.startswith(".seedbank/"):
            continue
        entries.append(Entry(kind="tree", sha=sha, path=path))
    if not entries:
        return 0
    lines, sig_line = sign_entries(key, entries, reviewer)
    append_block(root, lines, sig_line)
    record_blobs(root, with_bytes)
    return len(entries)


def init(root=".", with_reviewer=False, standards=None, baseline_opt=None):
    """Set up `.seedbank/` in root.

    with_reviewer: also create the approval key.
    standards:     list of standard links to put in bank.json.
    baseline_opt:  True = yes, False = no, None = ask.
    """
    root = Path(root).resolve()
    data = _ensure_bank(root, standards=standards)
    reviewer = _reviewer_login()
    pub = None
    if with_reviewer:
        reviewer, pub = create_reviewer_key(reviewer)
        data.setdefault("reviewers", {})[reviewer] = pub
        _write_bank(root, data)
        print(f"  added {reviewer} to reviewers")
    print(f"  wrote {bank_path(root).relative_to(root)}")
    if data.get("trust"):
        print(f"  trusting   {', '.join(data['trust'])}")
    if data.get("standards"):
        print(f"  standards  {', '.join(data['standards'])}")
    else:
        print("  standards  none yet; add one with: seedbank init --standard <link>")
    if ledger_path(root).is_file():
        return
    want_baseline = baseline_opt
    if want_baseline is None:
        count_est = sum(1 for p in Path(root).rglob("*")
                         if p.is_file() and not any(seg == ".git" or seg == ".seedbank" for seg in p.parts))
        if sys.stdin.isatty():
            answer = input(f"  baseline   {count_est} files exist. Mark them all as vetted now? [y/N] ")
            want_baseline = answer.strip().lower().startswith("y")
        else:
            want_baseline = False
    if want_baseline:
        if reviewer not in data.get("reviewers", {}):
            raise SeedbankError("add a reviewer with `seedbank init --reviewer` before the baseline")
        count = baseline(root, reviewer)
        print(f"  baseline   {count} entries signed and added to .seedbank/vetted")
