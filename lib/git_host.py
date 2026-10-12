import json
import os
import subprocess
import urllib.parse
import urllib.request

from lib.error import SeedbankError


class Granted:
    def __init__(self, users, note=None):
        self.users = users
        self.note = note


class Instructions:
    def __init__(self, repo, users):
        self.repo = repo
        self.users = users


def kind_of(repo):
    if repo.host == "github.com":
        return "github"
    if repo.host == "gitlab.com":
        return "gitlab"
    if repo.host == "codeberg.org":
        return "forgejo"
    return "other"


def _gh_json(method, path, payload=None):
    args = ["gh", "api", "--method", method, path]
    if payload:
        for key, value in payload.items():
            args.extend(["-f", f"{key}={value}"])
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SeedbankError((proc.stderr or proc.stdout).strip() or "GitHub API failed")
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


def _http_json(method, url, token, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise SeedbankError(detail or f"{url} returned {exc.code}") from exc
    return json.loads(body) if body else {}


def grant_read(repo, users):
    if not users:
        raise SeedbankError("name the people to share with")
    kind = kind_of(repo)
    if kind == "github":
        owner = _gh_json("GET", f"users/{repo.owner}")
        for user in users:
            _gh_json(
                "PUT",
                f"repos/{repo.owner}/{repo.repo}/collaborators/{user}",
                {"permission": "pull"},
            )
        note = None
        if owner.get("type") == "User":
            note = "a personal account cannot grant read without push; an organization can"
        return Granted(users, note)
    if kind == "gitlab":
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            return Instructions(repo, users)
        project = urllib.parse.quote(f"{repo.owner}/{repo.repo}", safe="")
        for user in users:
            _http_json(
                "POST",
                f"https://{repo.host}/api/v4/projects/{project}/members",
                token,
                {"username": user, "access_level": 20},
            )
        return Granted(users)
    if kind == "forgejo":
        token = os.environ.get("FORGEJO_TOKEN") or os.environ.get("GITEA_TOKEN")
        if not token:
            return Instructions(repo, users)
        for user in users:
            _http_json(
                "PUT",
                f"https://{repo.host}/api/v1/repos/{repo.owner}/{repo.repo}/collaborators/{user}",
                token,
                {"permission": "read"},
            )
        return Granted(users)
    return Instructions(repo, users)
