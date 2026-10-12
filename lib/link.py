from dataclasses import dataclass

from lib.error import SeedbankError


@dataclass(frozen=True)
class Link:
    host: str
    owner: str
    repo: str
    folder: str | None
    ref: str | None


@dataclass(frozen=True)
class Short:
    owner: str
    name: str


def parse_link(text):
    text = text.strip()
    if "/" not in text:
        raise SeedbankError(f"not a seed link: {text}")
    if text.count("/") == 1 and "." not in text.split("/", 1)[0]:
        owner, name = text.split("/", 1)
        if not owner or not name:
            raise SeedbankError(f"not a seed link: {text}")
        return Short(owner, name)
    if "@" in text:
        body, ref = text.rsplit("@", 1)
    else:
        body, ref = text, None
    if "//" in body:
        repo_part, folder = body.split("//", 1)
    else:
        repo_part, folder = body, None
    parts = repo_part.split("/")
    if len(parts) != 3 or not all(parts):
        raise SeedbankError(f"not a seed link: {text}")
    host, owner, repo = parts
    return Link(host, owner, repo, folder or None, ref or None)


def link_text(link):
    text = f"{link.host}/{link.owner}/{link.repo}"
    if link.folder:
        text += "//" + link.folder
    if link.ref:
        text += "@" + link.ref
    return text


def same_repo(link, repo):
    return (link.host, link.owner, link.repo) == (repo.host, repo.owner, repo.repo)
