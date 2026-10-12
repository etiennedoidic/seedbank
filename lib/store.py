import hashlib
import json
from pathlib import Path

from lib.link import link_text


def cache_root():
    path = Path.home() / ".cache" / "seedbank"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_root():
    path = Path.home() / ".local" / "share" / "seedbank"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _remember_path():
    return data_root() / "remembered.json"


def _load():
    path = _remember_path()
    if not path.is_file():
        return {"links": {}, "names": {}}
    return json.loads(path.read_text())


def _save(data):
    _remember_path().write_text(json.dumps(data, indent=2) + "\n")


def remember(seed_name, link, commit):
    data = _load()
    text = link_text(link)
    data["links"][text] = {"commit": commit, "link": text}
    data["names"][f"{link.owner}/{seed_name}"] = {"commit": commit, "link": text}
    _save(data)


def remembered_commit(link):
    text = link_text(link)
    return _load()["links"].get(text, {}).get("commit")


def remembered_name(owner, name):
    item = _load()["names"].get(f"{owner}/{name}")
    if not item:
        return None
    return item


def catalog_dir(url):
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    path = cache_root() / "catalogs" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def bare_dir(link):
    path = cache_root() / "repos" / link.host / link.owner / f"{link.repo}.git"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def tree_dir(commit, folder):
    label = folder.replace("/", "__") if folder else "_root"
    return cache_root() / "trees" / commit / label
