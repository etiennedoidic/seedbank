import json
import shutil
import subprocess

from lib.error import SeedbankError
from lib.git import git, parse_remote, remote_ref, run
from lib.link import Link, link_text, parse_link
from lib.store import catalog_dir, remembered_name

PUBLIC_URL = "https://github.com/etiennedoidic/seedbank.git"
PUBLIC_BRANCH = "catalog"

# Full row shape. Keys that aren't set at publish time are stored as empty.
ROW_FIELDS = (
    "name",
    "link",
    "commit",
    "tree",
    "kind",
    "description",
    "tags",
    "requires",
    "permissions",
    "dependencies",
    "members",
    "lane",
    "history",
    "delisted",
)


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
        try:
            rows.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
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


def lookup(short, url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    """Find a row by short name. Returns the stored row dict or None."""
    try:
        clone = open_catalog(url, branch)
    except SeedbankError:
        clone = None
    name = f"{short.owner}/{short.name}"
    if clone is not None:
        for row in _read_rows(clone):
            if row.get("name") == name:
                return row
    cached = remembered_name(short.owner, short.name)
    if cached:
        return {"name": name, "link": cached["link"], "commit": cached.get("commit", "")}
    return None


def search_rows(words, url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    """Match words against stored row fields. Pure read; skips delisted rows."""
    clone = open_catalog(url, branch)
    hits = []
    for row in _read_rows(clone):
        if row.get("delisted"):
            continue
        haystack = " ".join(
            [
                row.get("name", ""),
                row.get("kind", ""),
                row.get("description", ""),
                *(row.get("tags") or []),
            ]
        ).casefold()
        if all(word.casefold() in haystack for word in words):
            hits.append(row)
    return hits


def resolve(seed_arg, url=PUBLIC_URL, branch=PUBLIC_BRANCH, anyway=False):
    """Return (Link, row-or-None).

    A short name resolves through the catalog and pins to the row's commit.
    A hand-typed link is used as given and returns row=None.
    A delisted row raises unless anyway=True.
    """
    parsed = parse_link(seed_arg)
    if isinstance(parsed, Link):
        return parsed, None
    row = lookup(parsed, url, branch)
    if row is None:
        raise SeedbankError(f"no catalog row for {seed_arg}")
    if row.get("delisted") and not anyway:
        raise SeedbankError(
            f"{row['name']} was delisted: {row['delisted']}. Use --anyway to fetch it anyway."
        )
    link = parse_link(row["link"])
    commit = row.get("commit") or None
    pinned = Link(link.host, link.owner, link.repo, link.folder, commit)
    return pinned, row


def _blank_row(name, link):
    row = {key: None for key in ROW_FIELDS}
    row["name"] = name
    row["link"] = link_text(link)
    row["tags"] = []
    row["requires"] = []
    row["permissions"] = []
    row["dependencies"] = []
    row["members"] = []
    row["history"] = []
    row["lane"] = ""
    row["delisted"] = None
    return row


def build_row(name, link, commit, tree, seed, lane=""):
    """Compose a row from a seed and its pinned commit/tree."""
    row = _blank_row(name, link)
    row["commit"] = commit
    row["tree"] = tree
    row["kind"] = seed.kind
    row["description"] = seed.description
    row["tags"] = list(seed.tags)
    row["requires"] = list(seed.requires)
    row["permissions"] = list(seed.permissions)
    row["dependencies"] = list(seed.dependencies)
    row["members"] = list(seed.members or [])
    row["lane"] = lane
    return row


def _commit_file(clone, rel, row, message):
    path = clone / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {key: row.get(key) for key in ROW_FIELDS if row.get(key) not in (None, [])}
    serialisable["name"] = row["name"]
    serialisable["link"] = row["link"]
    if row.get("delisted"):
        serialisable["delisted"] = row["delisted"]
    path.write_text(json.dumps(serialisable, indent=2, sort_keys=False) + "\n")
    git(clone, "add", rel)
    proc = git(clone, "diff", "--cached", "--quiet", check=False)
    if proc.returncode == 0:
        return False
    git(clone, "commit", "-m", message)
    return True


def _fork_slug(slug):
    """Fork the catalog repo under the current user, return <login>/<repo>."""
    me = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True
    )
    if me.returncode != 0 or not me.stdout.strip():
        raise SeedbankError((me.stderr or "could not read your GitHub login").strip())
    login = me.stdout.strip()
    _, upstream_repo = slug.split("/", 1)
    fork_slug = f"{login}/{upstream_repo}"
    exists = subprocess.run(
        ["gh", "api", f"repos/{fork_slug}", "--jq", ".full_name"],
        capture_output=True, text=True,
    )
    if exists.returncode != 0 or not exists.stdout.strip():
        proc = subprocess.run(
            ["gh", "repo", "fork", slug, "--clone=false", "--remote=false"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise SeedbankError((proc.stderr or proc.stdout).strip() or "fork failed")
    return login, fork_slug


def _can_push(slug):
    proc = subprocess.run(
        ["gh", "api", f"repos/{slug}", "--jq", ".permissions.push"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False
    return proc.stdout.strip().lower() == "true"


def _open_pr(url, base, head, title, body):
    slug = _github_slug(url)
    if slug is None:
        host, owner, repo = parse_remote(url)
        return f"https://{host}/{owner}/{repo}/compare/{base}...{head}?expand=1"
    existing = subprocess.run(
        [
            "gh", "pr", "list",
            "--repo", slug,
            "--head", head,
            "--base", base,
            "--json", "url",
            "--jq", ".[0].url",
        ],
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


def _short(commit):
    return commit[:7] if commit else ""


def submit(row, catalog_url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    """Add or update a row in the catalog.

    row must carry name, link, commit and tree. Returns a dict:
      {"accepted": True|False, "request": url or None, "status": "same"|"updated"|"new"}
    """
    short_name = row["name"]
    rel = _row_path(short_name)

    if not branch_exists(catalog_url, branch):
        path = catalog_dir(catalog_url)
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        run(["git", "init", "-b", branch, str(path)])
        run(["git", "-C", str(path), "remote", "add", "origin", _push_url(catalog_url)])
        _commit_file(path, rel, row, f"Add {short_name}.")
        git(path, "push", "-u", "origin", branch)
        return {"accepted": True, "request": None, "status": "new"}

    clone = open_catalog(catalog_url, branch)
    current = clone / rel
    existing = None
    if current.is_file():
        try:
            existing = json.loads(current.read_text())
        except json.JSONDecodeError:
            existing = None

    if existing and existing.get("commit") == row.get("commit"):
        return {"accepted": True, "request": None, "status": "same"}

    history = list(existing.get("history") or []) if existing else []
    if existing and existing.get("commit"):
        history.append({"commit": existing["commit"], "tree": existing.get("tree")})
    row["history"] = history
    status = "updated" if existing else "new"

    slug = _github_slug(catalog_url)
    if slug and not _can_push(slug):
        login, fork_slug = _fork_slug(slug)
        git(clone, "remote", "set-url", "--push", "origin", _push_url(f"https://github.com/{fork_slug}.git"))
        head_on_fork = f"seedbank/{short_name}-{_short(row['commit']) or 'row'}"
        git(clone, "checkout", "-B", head_on_fork, f"origin/{branch}")
        message = _message_for(status, short_name, row)
        if not _commit_file(clone, rel, row, message):
            return {"accepted": True, "request": None, "status": "same"}
        git(clone, "push", "-u", "origin", head_on_fork)
        request = _open_pr(
            catalog_url,
            branch,
            f"{login}:{head_on_fork}",
            _title_for(status, short_name, row),
            f"link: {row['link']}\ncommit: {row.get('commit', '')}\n",
        )
        return {"accepted": False, "request": request, "status": status}

    head = f"seedbank/{short_name}-{_short(row['commit']) or 'row'}"
    if slug:
        git(clone, "remote", "set-url", "--push", "origin", _push_url(catalog_url))
    git(clone, "checkout", "-B", head, f"origin/{branch}")
    message = _message_for(status, short_name, row)
    if not _commit_file(clone, rel, row, message):
        return {"accepted": True, "request": None, "status": "same"}
    git(clone, "push", "-u", "origin", head)
    request = _open_pr(
        catalog_url,
        branch,
        head,
        _title_for(status, short_name, row),
        f"link: {row['link']}\ncommit: {row.get('commit', '')}\n",
    )
    return {"accepted": False, "request": request, "status": status}


def _title_for(status, short_name, row):
    short = _short(row.get("commit") or "")
    if status == "updated":
        return f"Update {short_name} to {short}" if short else f"Update {short_name}"
    if status == "new":
        return f"Add {short_name}"
    return f"{short_name}"


def _message_for(status, short_name, row):
    short = _short(row.get("commit") or "")
    if status == "updated":
        return f"Update {short_name} to {short}." if short else f"Update {short_name}."
    return f"Add {short_name}."


def delist_row(short_name, reason, catalog_url=PUBLIC_URL, branch=PUBLIC_BRANCH):
    """Open a PR that marks a row delisted."""
    clone = open_catalog(catalog_url, branch)
    rel = _row_path(short_name)
    current = clone / rel
    if not current.is_file():
        raise SeedbankError(f"no row for {short_name}")
    row = json.loads(current.read_text())
    if row.get("delisted") == reason:
        return {"accepted": True, "request": None, "status": "same"}
    row["delisted"] = reason

    slug = _github_slug(catalog_url)
    if slug and not _can_push(slug):
        login, fork_slug = _fork_slug(slug)
        git(clone, "remote", "set-url", "--push", "origin", _push_url(f"https://github.com/{fork_slug}.git"))
        head_on_fork = f"seedbank/delist-{short_name.replace('/', '-')}"
        git(clone, "checkout", "-B", head_on_fork, f"origin/{branch}")
        if not _commit_file(clone, rel, row, f"Delist {short_name}."):
            return {"accepted": True, "request": None, "status": "same"}
        git(clone, "push", "-u", "origin", head_on_fork)
        request = _open_pr(
            catalog_url, branch, f"{login}:{head_on_fork}",
            f"Delist {short_name}", f"reason: {reason}\n",
        )
        return {"accepted": False, "request": request, "status": "delisted"}

    head = f"seedbank/delist-{short_name.replace('/', '-')}"
    if slug:
        git(clone, "remote", "set-url", "--push", "origin", _push_url(catalog_url))
    git(clone, "checkout", "-B", head, f"origin/{branch}")
    if not _commit_file(clone, rel, row, f"Delist {short_name}."):
        return {"accepted": True, "request": None, "status": "same"}
    git(clone, "push", "-u", "origin", head)
    request = _open_pr(
        catalog_url, branch, head,
        f"Delist {short_name}", f"reason: {reason}\n",
    )
    return {"accepted": False, "request": request, "status": "delisted"}
