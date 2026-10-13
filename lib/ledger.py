"""The .seedbank/vetted ledger.

Format (one record per line):

    blob <sha>  <path>
    tree <sha>  <path>
    sig  <reviewer> <date> <base64 of ssh signature>

A `sig` covers every line since the previous `sig`. Unsigned or
badly-signed blocks don't count. Signatures are made and verified with
`ssh-keygen -Y sign` / `ssh-keygen -Y verify`, namespace `seedbank-vetted`.
"""

import base64
import json
import os
import subprocess
import tempfile
import datetime
from dataclasses import dataclass
from pathlib import Path

from lib.error import SeedbankError

NAMESPACE = "seedbank-vetted"


@dataclass
class Entry:
    kind: str          # "blob" | "tree"
    sha: str
    path: str


def ledger_path(root):
    return Path(root) / ".seedbank" / "vetted"


def blobs_dir(root):
    path = Path(root) / ".seedbank" / "blobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bank_path(root):
    return Path(root) / ".seedbank" / "bank.json"


def read_bank(root):
    path = bank_path(root)
    if not path.is_file():
        raise SeedbankError("no .seedbank/bank.json; run `seedbank init` first")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SeedbankError(f"bank.json is not valid JSON ({exc})") from exc


def _parse_entry(line):
    parts = line.rstrip().split(None, 2)
    if len(parts) < 3:
        return None
    kind, sha, path = parts
    if kind not in ("blob", "tree"):
        return None
    return Entry(kind=kind, sha=sha, path=path)


def _parse_sig(line):
    """`sig <reviewer> <date> <b64>` → (reviewer, date, b64) or None."""
    if not line.startswith("sig "):
        return None
    parts = line.split(None, 3)
    if len(parts) != 4:
        return None
    return parts[1], parts[2], parts[3]


def _blocks(lines):
    """Yield (block_lines, sig_tuple_or_None) for each block in `lines`."""
    buffer = []
    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("sig "):
            sig = _parse_sig(line)
            yield buffer, sig
            buffer = []
        elif line.strip() and not line.startswith("#"):
            buffer.append(line)
    if buffer:
        yield buffer, None


def _ssh_available():
    try:
        subprocess.run(["ssh-keygen", "-?"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return True


def _verify_block(block_lines, sig_tuple, reviewers):
    """Return True if the block was signed by one of the known reviewers."""
    if not sig_tuple:
        return False
    reviewer, _date, b64 = sig_tuple
    pub = reviewers.get(reviewer)
    if not pub:
        return False
    if not _ssh_available():
        return False
    content = "\n".join(block_lines) + "\n"
    try:
        sig_bytes = base64.b64decode(b64, validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    with tempfile.TemporaryDirectory(prefix="seedbank-verify-") as tmp:
        tmp = Path(tmp)
        allowed = tmp / "allowed_signers"
        allowed.write_text(f"{reviewer} {pub}\n")
        sigfile = tmp / "block.sig"
        sigfile.write_bytes(sig_bytes)
        proc = subprocess.run(
            [
                "ssh-keygen", "-Y", "verify",
                "-f", str(allowed),
                "-I", reviewer,
                "-n", NAMESPACE,
                "-s", str(sigfile),
            ],
            input=content, capture_output=True, text=True,
        )
        return proc.returncode == 0


def read_vetted(root, reviewers):
    """Walk the ledger. Return (entries, verified_set, problems)."""
    path = ledger_path(root)
    if not path.is_file():
        return [], set(), []
    text = path.read_text()
    lines = text.splitlines()
    entries = []
    verified = set()
    problems = []
    for block, sig in _blocks(lines):
        if not block and not sig:
            continue
        if not sig:
            problems.append("unsigned block at end of .seedbank/vetted")
            continue
        if not _verify_block(block, sig, reviewers):
            problems.append(f"ledger block signed by {sig[0]} does not verify")
            continue
        for ln in block:
            entry = _parse_entry(ln)
            if entry is None:
                continue
            entries.append(entry)
            verified.add(f"{entry.kind} {entry.sha}")
    return entries, verified, problems


def sign_entries(key_path, new_entries, reviewer):
    """Sign a list of Entry (append-only); return (text_block, sig_line)."""
    if not _ssh_available():
        raise SeedbankError("ssh-keygen is not available")
    date = datetime.date.today().isoformat()
    lines = []
    for entry in new_entries:
        lines.append(f"{entry.kind} {entry.sha}  {entry.path}")
    content = "\n".join(lines) + "\n"
    with tempfile.TemporaryDirectory(prefix="seedbank-sign-") as tmp:
        tmp = Path(tmp)
        payload = tmp / "block"
        payload.write_text(content)
        proc = subprocess.run(
            [
                "ssh-keygen", "-Y", "sign",
                "-f", str(key_path),
                "-n", NAMESPACE,
                str(payload),
            ],
            capture_output=False,  # ssh-keygen may prompt on the terminal
        )
        if proc.returncode != 0:
            raise SeedbankError("ssh-keygen -Y sign failed (wrong password?)")
        sig_file = payload.with_suffix(payload.suffix + ".sig")
        sig_bytes = sig_file.read_bytes()
    b64 = base64.b64encode(sig_bytes).decode()
    return lines, f"sig {reviewer} {date} {b64}"


def append_block(root, lines, sig_line):
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for line in lines:
            fh.write(line + "\n")
        fh.write(sig_line + "\n")


def record_blobs(root, entries_with_bytes):
    """Store a copy of each blob so we can diff and revert later."""
    out = blobs_dir(root)
    for entry, data in entries_with_bytes:
        if entry.kind != "blob":
            continue
        dest = out / entry.sha
        if dest.is_file():
            continue
        try:
            # Use git hash-object -w if we're in a repo, else just write.
            # The hash we store is already git's hash, so writing data is enough.
            dest.write_bytes(data)
        except OSError:
            continue


def read_recorded_blob(root, sha):
    path = blobs_dir(root) / sha
    if path.is_file():
        return path.read_bytes()
    return None
