import json
from dataclasses import dataclass
from pathlib import Path

from lib.error import SeedbankError
from lib.git import origin_url, parse_remote, toplevel

KINDS = ("tool", "app", "game", "component", "prompt", "skill", "bundle", "standard")
FIELDS = {
    "kind",
    "description",
    "name",
    "tags",
    "dependencies",
    "requires",
    "permissions",
    "members",
}
LIST_FIELDS = ("tags", "dependencies", "requires", "permissions", "members")


@dataclass
class Repo:
    root: Path
    url: str
    host: str
    owner: str
    repo: str


@dataclass
class Seed:
    folder: Path
    kind: str
    description: str
    name: str
    tags: list
    dependencies: list
    requires: list
    permissions: list
    members: list | None


def repo_of(folder):
    folder = Path(folder).resolve()
    root = toplevel(folder)
    url = origin_url(root)
    host, owner, repo = parse_remote(url)
    return Repo(root, url, host, owner, repo)


def default_name(folder):
    folder = Path(folder).resolve()
    repo = repo_of(folder)
    if folder == repo.root:
        return repo.repo
    return folder.name


def _strings(raw, key):
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SeedbankError(f"{key} must be a list of strings")
    return value


def read_seed(folder=None, name_hint=None):
    folder = Path(folder or ".").resolve()
    path = folder / "seed.json"
    if not path.is_file():
        raise SeedbankError(f"no seed.json in {folder}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SeedbankError(f"seed.json is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise SeedbankError("seed.json must be an object")
    unknown = [key for key in raw if key not in FIELDS]
    if unknown:
        raise SeedbankError(
            f"{unknown[0]} does not belong in seed.json; that comes from git"
        )
    for key in LIST_FIELDS:
        if key in raw:
            _strings(raw, key)
    kind = raw.get("kind")
    if not isinstance(kind, str):
        kind = ""
    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise SeedbankError("description must be a string")
    name = raw.get("name")
    if name is None:
        name = name_hint or default_name(folder)
    elif not isinstance(name, str) or not name:
        raise SeedbankError("name must be a string")
    return Seed(
        folder=folder,
        kind=kind,
        description=description,
        name=name,
        tags=raw.get("tags") or [],
        dependencies=raw.get("dependencies") or [],
        requires=raw.get("requires") or [],
        permissions=raw.get("permissions") or [],
        members=raw.get("members") if "members" in raw else None,
    )
