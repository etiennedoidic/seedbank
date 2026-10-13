"""One classifier used by check, publish, review and catalog CI.

Reads files only; never runs them. Picks one of lanes A, B, C, D (or no
lane when there's a fail-level problem).
"""

import ast
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lib.error import SeedbankError
from lib.link import Link, parse_link


# ---------- data ----------------------------------------------------------

CONTENT_EXT = {
    ".md", ".txt", ".json",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
}
# Standard text-only filenames that have no extension.
CONTENT_NAMES = {
    "LICENSE", "LICENCE", "COPYING", "COPYRIGHT", "NOTICE",
    "README", "AUTHORS", "CONTRIBUTORS", "CHANGELOG", "CONTRIBUTING",
}
SVG_EXT = {".svg"}
HTML_EXT = {".html", ".htm"}
CSS_EXT = {".css"}
JS_EXT = {".js", ".mjs"}
SCRIPT_EXT = {".py", ".sh", ".bash", ".zsh", ".rb", ".pl", ".ps1"}
MAX_SIZE = 1024 * 1024
MAX_NEW_SCRIPT_LINES = 300

# The exact line a locked-UI seed has to carry (verbatim, inside <head>).
LOCKED_CSP = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'self'; "
    "connect-src 'none'; "
    "img-src 'self' data:; "
    "form-action 'none'; "
    "base-uri 'none'"
    '">'
)

# Secret patterns, in roughly ascending order of specificity.
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "GitHub PAT"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"), "GitLab token"),
    (re.compile(r"\bxox[bp]-[A-Za-z0-9\-]{20,}\b"), "Slack token"),
    (re.compile(r"\btskey-[A-Za-z0-9\-]{20,}\b"), "Tailscale key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "API key starting with sk-"),
]

# Python ast names that mean "this script reaches outside this seed".
_PY_RISKY_IMPORTS = {
    "subprocess", "os.system", "socket", "urllib", "urllib.request",
    "urllib.parse", "http", "http.client", "requests", "httpx",
    "ctypes", "ssl",
}
_PY_RISKY_CALLS = {"eval", "exec", "compile", "__import__"}
# Shell patterns we flag.
_SH_RISKY = [
    (re.compile(r"\bcurl\b[^\n]*\|\s*(sh|bash|zsh)\b"), "curl | sh"),
    (re.compile(r"\bwget\b[^\n]*\|\s*(sh|bash|zsh)\b"), "wget | sh"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\brm\s+-rf?\s+/"), "rm -rf /"),
    (re.compile(r"\beval\b"), "eval"),
    (re.compile(r"\bnc\b"), "nc"),
    (re.compile(r"\blaunchctl\b"), "launchctl"),
    (re.compile(r"\bsystemctl\b"), "systemctl"),
]


@dataclass
class File:
    path: str
    blob: str
    size: int
    mode: str
    classification: str      # content | svg | html | css | js | script | binary | other
    status: str              # new | changed | vetted
    old_blob: str | None = None


@dataclass
class Problem:
    level: str               # fail | warn
    check: str
    file: str | None
    line: int | None
    message: str


@dataclass
class Report:
    lane: str                        # A | B | C | D | ""
    files: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    effective_permissions: list = field(default_factory=list)
    new_script_lines: int = 0
    skipped_trees: list = field(default_factory=list)

    def failing(self) -> bool:
        return any(p.level == "fail" for p in self.problems)


@dataclass(frozen=True)
class WorkingTree:
    folder: Path


@dataclass(frozen=True)
class CommitSource:
    bare: Path
    commit: str
    folder: str = ""


# ---------- git plumbing --------------------------------------------------


def _git(folder, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(folder), *args],
        check=check, capture_output=True, text=True,
    )


def _git_out(folder, *args):
    return _git(folder, *args).stdout


def _git_bytes(folder, *args):
    proc = subprocess.run(
        ["git", "-C", str(folder), *args],
        check=True, capture_output=True,
    )
    return proc.stdout


_LS_LINE = re.compile(r"^(\S+)\s+\S+\s+(\S+)\s+(\S+)\t(.*)$")
_LS_D_LINE = re.compile(r"^\S+\s+\S+\s+(\S+)\t(.*)$")


def _parse_ls_tree_lsize(text):
    files = []
    for line in text.splitlines():
        match = _LS_LINE.match(line)
        if not match:
            continue
        mode, sha, size, path = match.group(1), match.group(2), match.group(3), match.group(4)
        try:
            size_i = int(size)
        except ValueError:
            size_i = 0
        files.append((path, sha, size_i, mode))
    return files


def _parse_ls_tree_dirs(text):
    trees = []
    for line in text.splitlines():
        match = _LS_D_LINE.match(line)
        if not match:
            continue
        trees.append((match.group(2), match.group(1)))
    return trees


def _list_working(folder: Path):
    """Build a throwaway index to hash a working tree exactly like git would.

    Returns (root_tree, files, trees). Paths are relative to `folder`.
    """
    folder = Path(folder).resolve()
    with tempfile.TemporaryDirectory(prefix="seedbank-classify-") as tmp:
        work = Path(tmp) / "work"
        shutil.copytree(folder, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _git(work, "init", "-q", "-b", "main")
        _git(work, "add", "-A", "--", ".")
        root_tree = _git_out(work, "write-tree").strip()
        files = _parse_ls_tree_lsize(_git_out(work, "ls-tree", "-r", "-l", root_tree))
        trees = [("", root_tree)]
        trees.extend(_parse_ls_tree_dirs(_git_out(work, "ls-tree", "-r", "-d", root_tree)))
        return root_tree, files, trees


def _list_commit(bare: Path, commit: str, folder: str):
    base = f"{commit}:{folder}" if folder else commit
    root_tree = _git_out(bare, "rev-parse", f"{base}^{{tree}}").strip() if not folder else \
                _git_out(bare, "rev-parse", base).strip()
    files = _parse_ls_tree_lsize(_git_out(bare, "ls-tree", "-r", "-l", base))
    trees = [("", root_tree)]
    trees.extend(_parse_ls_tree_dirs(_git_out(bare, "ls-tree", "-r", "-d", base)))
    return root_tree, files, trees


# ---------- per-file checks ----------------------------------------------


def _classify_ext(path: str, mode: str) -> str:
    name = Path(path).name
    ext = Path(path).suffix.lower()
    # Known bare filenames are content.
    if name in CONTENT_NAMES or (not ext and name.upper() in CONTENT_NAMES):
        return "content"
    if ext in CONTENT_EXT:
        return "content"
    if ext in SVG_EXT:
        return "svg"
    if ext in HTML_EXT:
        return "html"
    if ext in CSS_EXT:
        return "css"
    if ext in JS_EXT:
        return "js"
    if ext in SCRIPT_EXT:
        return "script"
    # Executable bit: 100755
    if mode and mode.endswith("755"):
        return "script"
    return "other"


_BINARY_MAGICS = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf")


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    for magic in _BINARY_MAGICS:
        if data.startswith(magic):
            return True
    # NUL byte in the first chunk is a strong binary signal.
    return b"\x00" in data[:4096]


def _check_secrets(path, text, problems):
    for pattern, label in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            problems.append(Problem("fail", "secrets", path, line, f"looks like a {label}"))


_HTML_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)
_HTML_EVENT = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
_HTML_IFRAME = re.compile(r"<(iframe|object|embed)\b", re.IGNORECASE)
_HTML_META_REFRESH = re.compile(r"<meta\b[^>]*http-equiv\s*=\s*['\"]?refresh", re.IGNORECASE)
_HTML_EXT_SRC = re.compile(r"\b(src|href|srcset|poster|action|data)\s*=\s*['\"](?!['\"])\s*([^'\"]+)['\"]", re.IGNORECASE)
_HTML_CSP = re.compile(r"<meta\b[^>]*http-equiv\s*=\s*['\"]?content-security-policy['\"]?[^>]*>", re.IGNORECASE)


def _external_target(url: str) -> bool:
    url = url.strip()
    if not url:
        return False
    low = url.lower()
    if low.startswith(("data:", "mailto:", "javascript:", "#")):
        return False
    if low.startswith(("http://", "https://", "//", "ftp://", "ws://", "wss://")):
        return True
    return False


def _check_html(path, text, problems):
    """Enforce lane B: either no JS at all with no external targets, or
    the exact locked CSP line before any script/style/link."""
    has_script = bool(_HTML_SCRIPT.search(text))
    has_event = bool(_HTML_EVENT.search(text))
    has_iframe = bool(_HTML_IFRAME.search(text))
    has_meta_refresh = bool(_HTML_META_REFRESH.search(text))

    externals = [m.group(2) for m in _HTML_EXT_SRC.finditer(text) if _external_target(m.group(2))]

    if not has_script and not has_event:
        # Plain HTML path: must have no external targets and no iframe/refresh.
        if externals:
            problems.append(Problem("fail", "no-network-ui", path, None,
                                    f"external reference blocks lane B: {externals[0]}"))
        if has_iframe:
            problems.append(Problem("fail", "no-network-ui", path, None,
                                    "<iframe>, <object> or <embed> blocks lane B"))
        if has_meta_refresh:
            problems.append(Problem("fail", "no-network-ui", path, None,
                                    "<meta http-equiv=refresh> blocks lane B"))
        return

    # Has <script> or inline events: needs the locked CSP, verbatim, before
    # the first <script>, <link> or <style>.
    csp_match = _HTML_CSP.search(text)
    if not csp_match:
        problems.append(Problem("warn", "no-network-ui", path, None,
                                "no Content-Security-Policy; add the locked line to list without review"))
        return
    if csp_match.group(0).strip() != LOCKED_CSP:
        problems.append(Problem("fail", "no-network-ui", path, None,
                                "CSP is not the exact locked line; see the review doc"))
        return
    head_end = text.lower().find("</head>")
    first_asset = min(
        (i for i in (text.lower().find(tag) for tag in ("<script", "<link", "<style")) if i >= 0),
        default=-1,
    )
    if first_asset >= 0 and csp_match.start() > first_asset:
        problems.append(Problem("fail", "no-network-ui", path, None,
                                "locked CSP must come before the first <script>, <link> or <style>"))
    if head_end >= 0 and csp_match.start() > head_end:
        problems.append(Problem("fail", "no-network-ui", path, None,
                                "locked CSP must be inside <head>"))


_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^'\")\s]+)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\s+(['\"])([^'\"]+)\1", re.IGNORECASE)


def _check_css(path, text, problems):
    for match in _CSS_URL.finditer(text):
        if _external_target(match.group(2)):
            problems.append(Problem("fail", "no-network-ui", path, None,
                                    f"url(...) points outside the seed: {match.group(2)}"))
            return
    for match in _CSS_IMPORT.finditer(text):
        if _external_target(match.group(2)):
            problems.append(Problem("fail", "no-network-ui", path, None,
                                    f"@import outside the seed: {match.group(2)}"))
            return


def _check_svg(path, text, problems):
    low = text.lower()
    if "<script" in low or "onload=" in low or "onclick=" in low or "<foreignobject" in low:
        problems.append(Problem("fail", "no-network-ui", path, None,
                                "svg has script, event handler, or <foreignObject>"))
        return
    if "xlink:href=\"http" in low or "href=\"http" in low:
        problems.append(Problem("fail", "no-network-ui", path, None,
                                "svg links outside the seed"))


def _python_flags(path, text, problems):
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        problems.append(Problem("warn", "script-flags", path, None, "python did not parse"))
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _PY_RISKY_IMPORTS or alias.name in _PY_RISKY_IMPORTS:
                    problems.append(Problem("warn", "script-flags", path, node.lineno,
                                            f"imports {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] in _PY_RISKY_IMPORTS or mod in _PY_RISKY_IMPORTS:
                problems.append(Problem("warn", "script-flags", path, node.lineno,
                                        f"imports from {mod}"))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _PY_RISKY_CALLS:
                problems.append(Problem("warn", "script-flags", path, node.lineno,
                                        f"calls {fn.id}"))


def _shell_flags(path, text, problems):
    for pattern, label in _SH_RISKY:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            problems.append(Problem("warn", "script-flags", path, line, f"shell uses {label}"))


# ---------- vetting / status ---------------------------------------------


def _under_skipped(path: str, skipped_prefixes) -> bool:
    for prefix in skipped_prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _blobs_at(source: CommitSource, commit: str) -> dict:
    base = f"{commit}:{source.folder}" if source.folder else commit
    try:
        ls = _git_out(source.bare, "ls-tree", "-r", "-l", base)
    except subprocess.CalledProcessError:
        return {}
    out = {}
    for path, sha, _size, _mode in _parse_ls_tree_lsize(ls):
        out[path] = sha
    return out


# ---------- lane rule ----------------------------------------------------


def _pick_lane(files, problems, perms, new_script_lines):
    if any(p.level == "fail" for p in problems):
        return ""
    new = [f for f in files if f.status in ("new", "changed")]
    if not new:
        return "A"
    kinds = {f.classification for f in new}
    has_binary = "binary" in kinds or "other" in kinds
    has_script = "script" in kinds
    has_flag = any(p.level == "warn" and p.check == "script-flags" for p in problems)
    has_warn_binary = any(p.level == "warn" and p.check == "binaries" for p in problems)
    if "admin" in perms or "net" in perms:
        return "D"
    if has_binary or has_warn_binary:
        return "D"
    if has_flag:
        return "D"
    if has_script and new_script_lines > MAX_NEW_SCRIPT_LINES:
        return "D"
    content_only = kinds.issubset({"content", "svg"})
    svg_safe = all(_safe_svg(f) for f in new if f.classification == "svg")
    if content_only and svg_safe:
        return "A"
    locked_ok = kinds.issubset({"content", "svg", "html", "css", "js"}) and \
                _every_html_locked(new, problems) and \
                _every_css_local(new, problems) and \
                svg_safe
    if locked_ok:
        return "B"
    if has_script and new_script_lines <= MAX_NEW_SCRIPT_LINES:
        return "C"
    return "C"


def _safe_svg(file):
    # If an svg has a no-network-ui failure recorded, _pick_lane already
    # saw the fail and returned "". Here we assume cleanness.
    return True


def _every_html_locked(new_files, problems):
    htmls = [f for f in new_files if f.classification == "html"]
    if not htmls:
        return True
    bad = {p.file for p in problems if p.check == "no-network-ui" and p.file}
    return all(f.path not in bad for f in htmls)


def _every_css_local(new_files, problems):
    csses = [f for f in new_files if f.classification == "css"]
    if not csses:
        return True
    bad = {p.file for p in problems if p.check == "no-network-ui" and p.file}
    return all(f.path not in bad for f in csses)


# ---------- the entry point ----------------------------------------------


def classify(source, vetted, seed, previous=None) -> Report:
    """Classify a seed source into a lane.

    source   -- WorkingTree(folder) or CommitSource(bare, commit, folder)
    vetted   -- set of strings like "blob <sha>" and "tree <sha>"
    seed     -- parsed Seed (we use permissions, dependencies, members)
    previous -- optional {"commit": sha} for a catalog update
    """
    if isinstance(source, WorkingTree):
        _root_tree, raw_files, trees = _list_working(source.folder)

        def read_blob(path, _sha):
            try:
                return (source.folder / path).read_bytes()
            except OSError:
                return b""
    elif isinstance(source, CommitSource):
        _root_tree, raw_files, trees = _list_commit(source.bare, source.commit, source.folder)

        def read_blob(_path, sha):
            try:
                return _git_bytes(source.bare, "cat-file", "-p", sha)
            except subprocess.CalledProcessError:
                return b""
    else:
        raise SeedbankError("classify: source must be WorkingTree or CommitSource")

    problems = []
    files = []
    skipped_trees = []
    skipped_prefixes = []
    for path, sha in trees:
        if path and f"tree {sha}" in vetted:
            skipped_trees.append((path, sha))
            skipped_prefixes.append(path)

    prior_blobs = {}
    changed_in_update = None
    if isinstance(source, CommitSource) and previous and previous.get("commit"):
        prior_blobs = _blobs_at(source, previous["commit"])
        try:
            diff = _git_out(
                source.bare, "diff", "--name-only",
                previous["commit"], source.commit, "--",
                source.folder or ".",
            )
            prefix = (source.folder + "/") if source.folder else ""
            changed_in_update = set()
            for line in diff.splitlines():
                if source.folder:
                    if line.startswith(prefix):
                        changed_in_update.add(line[len(prefix):])
                else:
                    changed_in_update.add(line)
        except subprocess.CalledProcessError:
            changed_in_update = None

    # Build the File list.
    for path, sha, size, mode in raw_files:
        if _under_skipped(path, skipped_prefixes):
            files.append(File(path, sha, size, mode, _classify_ext(path, mode), "vetted"))
            continue

        if mode and mode.startswith("120"):
            problems.append(Problem("fail", "size-shape", path, None,
                                    "symlink; seeds cannot contain symlinks"))
            continue
        if mode and mode.startswith("160"):
            problems.append(Problem("fail", "size-shape", path, None,
                                    "submodule; seeds cannot contain submodules"))
            continue
        if size > MAX_SIZE:
            problems.append(Problem("fail", "size-shape", path, None,
                                    f"{size} bytes; keep under 1 MiB or use Git LFS"))

        kind = _classify_ext(path, mode)

        # Status
        if f"blob {sha}" in vetted:
            status = "vetted"
            old = None
        elif path in prior_blobs and prior_blobs[path] != sha:
            status = "changed"
            old = prior_blobs[path]
        elif path in prior_blobs and prior_blobs[path] == sha:
            status = "vetted"
            old = None
        else:
            status = "new"
            old = None

        if changed_in_update is not None and status == "new" and path not in changed_in_update:
            status = "vetted"

        files.append(File(path, sha, size, mode, kind, status, old_blob=old))

    # Per-file checks on new or changed.
    new_or_changed = [f for f in files if f.status in ("new", "changed")]
    new_script_lines = 0

    for file in new_or_changed:
        data = read_blob(file.path, file.blob)
        if _looks_binary(data) and file.classification not in ("content",):
            file.classification = "binary"
            problems.append(Problem("warn", "binaries", file.path, None, "committed binary"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None

        name = Path(file.path).name
        if name == ".env":
            problems.append(Problem("fail", "secrets", file.path, None, ".env file"))

        if text is not None:
            _check_secrets(file.path, text, problems)
            if file.classification == "html":
                _check_html(file.path, text, problems)
            elif file.classification == "css":
                _check_css(file.path, text, problems)
            elif file.classification == "svg":
                _check_svg(file.path, text, problems)
            elif file.classification == "script":
                if file.path.endswith(".py"):
                    _python_flags(file.path, text, problems)
                else:
                    _shell_flags(file.path, text, problems)
                new_script_lines += text.count("\n") + 1
            elif file.classification == "js":
                new_script_lines += text.count("\n") + 1

    # Dependencies and members must be pinned.
    for ref_label, refs in (("dependency", seed.dependencies), ("member", seed.members or [])):
        for item in refs:
            try:
                link = parse_link(item)
            except SeedbankError:
                problems.append(Problem("fail", "dependencies", None, None,
                                        f"{item} is not a seed link"))
                continue
            if not isinstance(link, Link):
                problems.append(Problem("fail", "dependencies", None, None,
                                        f"{item}: use host/owner/repo, not a short name"))
                continue
            if not link.ref:
                problems.append(Problem("fail", "dependencies", None, None,
                                        f"{item}: pin with @tag or @commit"))

    # Effective permissions (phase 3 will add nested seeds' perms).
    perms = sorted(set(seed.permissions))

    lane = _pick_lane(files, problems, set(perms), new_script_lines)
    return Report(
        lane=lane,
        files=files,
        problems=problems,
        effective_permissions=perms,
        new_script_lines=new_script_lines,
        skipped_trees=skipped_trees,
    )
