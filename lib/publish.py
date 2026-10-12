from lib.catalog import PUBLIC_BRANCH, PUBLIC_URL, submit
from lib.check import check
from lib.error import SeedbankError
from lib.git import publicly_readable, remote_has_commit
from lib.link import Link, link_text
from lib.read_seed import repo_of


def _build(folder, pinned):
    repo = repo_of(folder)
    rel = folder.resolve().relative_to(repo.root).as_posix()
    folder_part = None if rel == "." else rel
    ref = None
    if pinned:
        from lib.git import git, git_out

        tags = [
            line
            for line in git(repo.root, "tag", "--points-at", "HEAD", check=False).stdout.splitlines()
            if line
        ]
        ref = tags[0] if tags else git_out(repo.root, "rev-parse", "HEAD").strip()
    return Link(repo.host, repo.owner, repo.repo, folder_part, ref), repo


def build_link(folder, pinned):
    link, _repo = _build(folder, pinned)
    return link


def publish(folder=None, catalog_url=PUBLIC_URL):
    seed, problems, _license = check(folder)
    if problems:
        for problem in problems:
            print(f"  {problem}")
        raise SeedbankError(f"{len(problems)} problem(s).")
    repo = repo_of(seed.folder)
    if not publicly_readable(repo.host, repo.owner, repo.repo):
        raise SeedbankError("push the repo somewhere public first")
    remote_has_commit(seed.folder)
    link = build_link(seed.folder, pinned=False)
    short_name = f"{repo.owner}/{seed.name}"
    branch = PUBLIC_BRANCH if catalog_url == PUBLIC_URL else "main"
    if catalog_url != PUBLIC_URL:
        from lib.git import default_branch_name

        branch = default_branch_name(catalog_url)
    result = submit(link, short_name, catalog_url, branch)
    print(f"  {'submitted':<10} {link_text(link)}")
    if result["accepted"]:
        print(f"  {'name':<10} {short_name}")
    else:
        print(f"  {'name':<10} {short_name} (once accepted)")
        if result["request"]:
            print(f"  {'request':<10} {result['request']}")
    return result
