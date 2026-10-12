import io
import shutil
import tarfile
from pathlib import Path

from lib.error import SeedbankError
from lib.git import git, git_out, https_url, run, ssh_url
from lib.link import Link, link_text
from lib.read_seed import read_seed
from lib.store import bare_dir, remember, remembered_commit, tree_dir

PLAIN = {
    "net": "network",
    "fs.read": "read files",
    "fs.write": "write files",
    "admin": "change system settings",
}


def _urls(link):
    return [
        https_url(link.host, link.owner, link.repo),
        ssh_url(link.host, link.owner, link.repo),
    ]


def _bare(link):
    path = bare_dir(link)
    urls = _urls(link)
    if not path.exists():
        last = None
        for url in urls:
            try:
                run(["git", "clone", "--bare", url, str(path)], timeout=180)
                return path
            except SeedbankError as exc:
                last = exc
                if path.exists():
                    shutil.rmtree(path)
        raise last
    last = None
    for url in urls:
        proc = run(
            ["git", "-C", str(path), "fetch", url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
            check=False,
            timeout=180,
        )
        if proc.returncode == 0:
            return path
        last = SeedbankError((proc.stderr or proc.stdout).strip() or "could not fetch")
    raise last


def _default_branch(bare):
    proc = git(bare, "symbolic-ref", "HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip().startswith("refs/heads/"):
        return proc.stdout.strip()[len("refs/heads/") :]
    raise SeedbankError("the repo has no default branch")


def _resolve(bare, link):
    if link.ref:
        return git_out(bare, "rev-parse", "--verify", f"{link.ref}^{{commit}}").strip()
    branch = _default_branch(bare)
    return git_out(bare, "rev-parse", f"refs/heads/{branch}").strip()


def pin(link):
    try:
        bare = _bare(link)
    except SeedbankError:
        return _offline_commit(link)
    try:
        return _resolve(bare, link)
    except SeedbankError as exc:
        ref = link.ref or "the default branch"
        raise SeedbankError(f"no commit for {ref}") from exc


def _offline_commit(link):
    commit = remembered_commit(link)
    bare = bare_dir(link)
    if commit and bare.exists() and git(bare, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0:
        return commit
    if (
        link.ref
        and bare.exists()
        and git(bare, "cat-file", "-e", f"{link.ref}^{{commit}}", check=False).returncode == 0
    ):
        return git_out(bare, "rev-parse", "--verify", f"{link.ref}^{{commit}}").strip()
    raise SeedbankError("this seed is not on disk")


def checkout(link, commit, folder):
    dest = tree_dir(commit, folder or "")
    if (dest / "seed.json").is_file():
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    raw = _archive_bytes(link, commit, folder)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        archive.extractall(dest, filter="data")
    if folder:
        nested = dest.joinpath(*folder.split("/"))
        if nested.is_dir():
            for child in list(nested.iterdir()):
                child.rename(dest / child.name)
            parent = nested
            while parent != dest:
                parent.rmdir()
                parent = parent.parent
    return dest


def _archive_bytes(link, commit, folder):
    import subprocess

    args = ["git", "-C", str(bare_dir(link)), "archive", commit]
    if folder:
        args.append(folder)
    proc = subprocess.run(args, capture_output=True, timeout=180)
    if proc.returncode != 0:
        raise SeedbankError(proc.stderr.decode().strip() or "git archive failed")
    return proc.stdout


def signer_of(link, commit):
    bare = bare_dir(link)
    if not bare.exists():
        return None
    proc = git(bare, "log", "-1", "--format=%G?%n%GS", commit, check=False)
    if proc.returncode != 0:
        return None
    status, _, name = proc.stdout.partition("\n")
    name = name.strip()
    if status.strip() == "N" or not name:
        return None
    return name


def tag_of(link, commit):
    bare = bare_dir(link)
    proc = git(bare, "tag", "--points-at", commit, check=False)
    if proc.returncode != 0:
        return None
    tags = [line for line in proc.stdout.splitlines() if line]
    return tags[0] if tags else None


def get(seed_arg):
    from lib.catalog import resolve

    link = resolve(seed_arg)
    commit = pin(link)
    files = checkout(link, commit, link.folder)
    hint = Path(link.folder).name if link.folder else link.repo
    seed = read_seed(files, name_hint=hint)
    followed = Link(link.host, link.owner, link.repo, link.folder, None)
    remember(seed.name, followed, commit)
    return files, seed, link, commit


def plain_permission(permission):
    return PLAIN.get(permission, permission)


def from_line(link, commit):
    short = commit[:7]
    tag = tag_of(link, commit)
    signer = signer_of(link, commit)
    signed = f", signed by {signer}" if signer else ""
    shown = link_text(Link(link.host, link.owner, link.repo, link.folder, None))
    if tag:
        return f"{shown} @ {tag} (commit {short}{signed})"
    if signed:
        return f"{shown} @ {short} ({signed[2:]})"
    return f"{shown} @ {short}"


def fetch(seed_arg, dest=None):
    files, seed, link, commit = get(seed_arg)
    print(f"  {seed.kind:<10} {seed.description}")
    print(f"  {'from':<10} {from_line(link, commit)}")
    needs = ", ".join(seed.requires) if seed.requires else "nothing"
    asks = ", ".join(plain_permission(item) for item in seed.permissions) if seed.permissions else "nothing"
    print(f"  {'needs':<10} {needs}")
    print(f"  {'asks':<10} {asks}")
    if "admin" in seed.permissions:
        print("  fetched only; you run it yourself, following SKILL.md")
    if dest is None:
        dest = Path.cwd() / seed.name
    else:
        dest = Path(dest)
    if dest.exists():
        raise SeedbankError(f"{dest} already exists")
    shutil.copytree(files, dest)
    print(f"  {'wrote':<10} {dest}")
    return dest
