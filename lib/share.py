from lib.git_host import Granted, Instructions, grant_read
from lib.link import link_text
from lib.publish import build_link
from lib.read_seed import repo_of
from lib.error import SeedbankError


def share(users, folder=None):
    from pathlib import Path

    folder = Path(folder or ".").resolve()
    repo = repo_of(folder)
    if folder != repo.root:
        raise SeedbankError("access covers the whole repo; keep this private seed in its own repo")
    outcome = grant_read(repo, users)
    link = build_link(folder, pinned=True)
    where = f"{repo.host}/{repo.owner}/{repo.repo}"
    if isinstance(outcome, Instructions):
        names = ", ".join(outcome.users)
        print(f"  on {where}, give {names} read access")
    elif isinstance(outcome, Granted):
        print(f"  {'gave':<10} {', '.join(outcome.users)} read access on {where}")
        if outcome.note:
            print(f"  {outcome.note}")
    print(f"  {'link':<10} {link_text(link)}")
    return link_text(link)
