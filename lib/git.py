import os
import subprocess
from pathlib import Path

from lib.error import SeedbankError


def run(args, cwd=None, check=True, timeout=120):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SeedbankError(f"{args[0]} timed out") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SeedbankError(detail or "command failed: " + " ".join(args))
    return proc


def git(folder, *args, check=True):
    return run(["git", "-C", str(folder), *args], check=check)


def git_out(folder, *args):
    return git(folder, *args).stdout


def toplevel(folder):
    return Path(git_out(folder, "rev-parse", "--show-toplevel").strip())


def origin_url(folder):
    proc = git(folder, "remote", "get-url", "origin", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SeedbankError("this repo has no origin remote")
    return proc.stdout.strip()


def parse_remote(url):
    text = url.strip()
    if text.startswith("git@"):
        host, path = text[4:].split(":", 1)
    elif "://" in text:
        rest = text.split("://", 1)[1]
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
        host, path = rest.split("/", 1)
    else:
        raise SeedbankError(f"cannot read remote {url}")
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        raise SeedbankError(f"remote {url} is not host/owner/repo")
    return host, parts[0], parts[1]


def https_url(host, owner, repo):
    return f"https://{host}/{owner}/{repo}.git"


def ssh_url(host, owner, repo):
    return f"git@{host}:{owner}/{repo}.git"


def dirty(folder):
    root = toplevel(folder)
    rel = Path(folder).resolve().relative_to(root).as_posix()
    out = git_out(root, "status", "--porcelain", "--untracked-files=normal")
    if rel in ("", "."):
        return bool(out.strip())
    prefix = rel + "/"
    for line in out.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path == rel or path.startswith(prefix):
            return True
    return False


def committed_file(folder, relpath):
    root = toplevel(folder)
    spec = (Path(folder).resolve().relative_to(root) / relpath).as_posix()
    if spec.startswith("./"):
        spec = spec[2:]
    if not (root / spec).is_file():
        return False
    if git(root, "cat-file", "-e", f"HEAD:{spec}", check=False).returncode != 0:
        return False
    return git(root, "diff", "--quiet", "HEAD", "--", spec, check=False).returncode == 0


def head_commit(folder):
    return git_out(folder, "rev-parse", "HEAD").strip()


def current_branch(folder):
    branch = git_out(folder, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        raise SeedbankError("this commit is not on a branch")
    return branch


def remote_has_commit(folder):
    branch = current_branch(folder)
    head = head_commit(folder)
    proc = git(folder, "ls-remote", "origin", f"refs/heads/{branch}", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SeedbankError(f"push {branch} first; the catalog follows the remote")
    remote_sha = proc.stdout.split()[0]
    if remote_sha != head:
        raise SeedbankError("push this commit first; the catalog follows the remote")


def publicly_readable(host, owner, repo):
    url = https_url(host, owner, repo)
    proc = run(
        ["git", "-c", "credential.helper=", "ls-remote", url, "HEAD"],
        check=False,
        timeout=30,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def remote_ref(url, ref):
    proc = run(["git", "ls-remote", url, ref], check=False, timeout=30)
    if proc.returncode != 0:
        raise SeedbankError((proc.stderr or proc.stdout).strip() or f"cannot reach {url}")
    return proc.stdout.strip()


def default_branch_name(url):
    proc = run(["git", "ls-remote", "--symref", url, "HEAD"], check=False, timeout=30)
    if proc.returncode != 0:
        raise SeedbankError((proc.stderr or "cannot read the default branch").strip())
    for line in proc.stdout.splitlines():
        if line.startswith("ref:"):
            name = line.split()[1]
            if name.startswith("refs/heads/"):
                return name[len("refs/heads/") :]
    raise SeedbankError(f"{url} has no default branch")
