import json
import shutil
import subprocess

from lib.error import SeedbankError
from lib.git import git, parse_remote, remote_ref, run
from lib.link import Link, link_text, parse_link
from lib.store import catalog_dir, remembered_name

PUBLIC_URL = "https://github.com/etiennedoidic/seedbank.git"
PUBLIC_BRANCH = "catalog"


def _push_url(url):
    host, owner, repo = parse_remote(url)
    if host == "github.com":
        return f"git@{host}:{owner}/{repo}.git"
    return url


def _github_slug(url):
    host, owner, repo = parse_remote(url)
    if host != "github.com":
        return None
    return f"{owner}/{repo}"


def branch_exists(url, branch):
    try:
        text = remote_ref(url, f"refs/heads/{branch}")
    except SeedbankError:
        return False
    return bool(text)


def _row_path(short_name):
    owner, name = short_name.split("/", 1)
    return f"rows/{owner}/{name}.json"


def _read_rows(clone):
    rows = []
    root = clone / "rows"
    if not root.is_dir():
        return rows
    for path in sorted(root.rglob("*.json")):
        rows.append(json.loads(path.read_text()))
    return rows


def open_catalog(url, branch):
    path = catalog_dir(url)
    if not (path / ".git").exists():
        if not branch_exists(url, branch):
            raise SeedbankError("no copy of the catalog yet")
        run(["git", "clone", "--branch", branch, "--single-branch", url, str(path)], timeout=180)
        return path
    fetched = run(
        ["git", "-C", str(path), "fetch", "origin", branch],
        check=False,
        timeout=180,
    )
    if fetched.returncode != 0:
        if not (path / "rows").exists():
            raise SeedbankError("no copy of the catalog yet")
        return path
    git(path, "checkout", branch)
    git(path, "reset", "--hard", "FETCH_HEAD")
    return path


def _refresh(row):
    from lib.fetch import get

    try:
        _files, seed, _link, commit = get(row["link"])
    except SeedbankError:
        cached = row.get("description")
        if cached:
            return row
        raise
    row["kind"] = seed.kind
    row["description"] = seed.description
    row["tags"] = list(seed.tags)
    row["commit"] = commit
    return row


def lookup(short, url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    try:
        clone = open_catalog(url, branch)
    except SeedbankError:
        clone = None
    name = f"{short.owner}/{short.name}"
    if clone is not None:
        for row in _read_rows(clone):
            if row.get("name") == name:
                return _refresh(row)
    cached = remembered_name(short.owner, short.name)
    if cached:
        return {"name": name, "link": cached["link"], "description": "", "tags": [], "kind": ""}
    return None


def search_rows(words, url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    clone = open_catalog(url, branch)
    hits = []
    for row in _read_rows(clone):
        try:
            row = _refresh(row)
        except SeedbankError:
            if not row.get("description"):
                continue
        haystack = " ".join(
            [row.get("name", ""), row.get("kind", ""), row.get("description", ""), *row.get("tags", [])]
        ).casefold()
        if all(word.casefold() in haystack for word in words):
            hits.append(row)
    return hits


def resolve(seed_arg, url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    parsed = parse_link(seed_arg)
    if not isinstance(parsed, Link):
        row = lookup(parsed, url, branch)
        if row is None:
            raise SeedbankError(f"no catalog row for {seed_arg}")
        return parse_link(row["link"])
    return parsed


def _commit_row(clone, rel, row, message):
    path = clone / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2) + "\n")
    git(clone, "add", rel)
    proc = git(clone, "diff", "--cached", "--quiet", check=False)
    if proc.returncode == 0:
        return False
    git(clone, "commit", "-m", message)
    return True


def _open_pr(url, base, head, title, body):
    slug = _github_slug(url)
    if slug is None:
        host, owner, repo = parse_remote(url)
        return f"https://{host}/{owner}/{repo}/compare/{base}...{head}?expand=1"
    existing = subprocess.run(
        ["gh", "pr", "list", "--repo", slug, "--head", head, "--base", base, "--json", "url", "--jq", ".[0].url"],
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        return existing.stdout.strip()
    created = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", slug,
            "--base", base,
            "--head", head,
            "--title", title,
            "--body", body,
        ],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()
        raise SeedbankError(detail or "could not open a pull request")
    return created.stdout.strip()


def submit(link, short_name, catalog_url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    if link.ref:
        raise SeedbankError("a catalog link follows the repo; it has no @ref")
    row = {"name": short_name, "link": link_text(link)}
    rel = _row_path(short_name)
    message = f"Add {short_name}."

    if not branch_exists(catalog_url, branch):
        path = catalog_dir(catalog_url)
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        run(["git", "init", "-b", branch, str(path)])
        run(["git", "-C", str(path), "remote", "add", "origin", _push_url(catalog_url)])
        _commit_row(path, rel, row, message)
        git(path, "push", "-u", "origin", branch)
        return {"accepted": True, "request": None}

    clone = open_catalog(catalog_url, branch)
    current = clone / rel
    if current.is_file() and json.loads(current.read_text()).get("link") == row["link"]:
        return {"accepted": True, "request": None}

    head = "seedbank/" + short_name
    git(clone, "remote", "set-url", "--push", "origin", _push_url(catalog_url))
    git(clone, "checkout", "-B", head, f"origin/{branch}")
    if not _commit_row(clone, rel, row, message):
        return {"accepted": True, "request": None}
    git(clone, "push", "-u", "origin", head)
    request = _open_pr(
        catalog_url,
        branch,
        head,
        f"Add {short_name}",
        f"link: {row['link']}\n",
    )
    return {"accepted": False, "request": request}
