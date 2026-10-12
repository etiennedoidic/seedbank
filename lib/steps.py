import re

from lib.error import SeedbankError
from lib.fetch import get
from lib.link import parse_link
from lib.store import remembered_name

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)+(?://[A-Za-z0-9._/-]+)?(?:@[A-Za-z0-9._-]+)?")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _helper(step):
    for token in _TOKEN.findall(step):
        try:
            parsed = parse_link(token)
        except SeedbankError:
            continue
        if hasattr(parsed, "host"):
            return token
        if _SLUG.match(parsed.owner) and _SLUG.match(parsed.name):
            return token
    return None


def _section(text):
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "## Integrate":
            start = index + 1
            break
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if line.startswith("## ") and not line.startswith("###"):
            break
        body.append(line)
    return "\n".join(body)


def integrate_block(folder, env):
    skill = folder / "SKILL.md"
    readme = folder / "README.md"
    if skill.is_file():
        text = skill.read_text()
    elif readme.is_file():
        text = readme.read_text()
    else:
        raise SeedbankError("no SKILL.md or README.md")
    section = _section(text)
    if section is None:
        raise SeedbankError("no Integrate section")
    heading = f"### {env}"
    lines = section.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        raise SeedbankError(f"no Integrate steps for {env}")
    steps = []
    for line in lines[start:]:
        if line.startswith("### "):
            break
        stripped = line.strip()
        if stripped:
            steps.append(stripped)
    return steps


def materialize(seed_arg):
    try:
        parsed = parse_link(seed_arg)
    except SeedbankError:
        parsed = None
    if parsed is not None and not hasattr(parsed, "host"):
        cached = remembered_name(parsed.owner, parsed.name)
        if cached:
            seed_arg = cached["link"]
    files, _seed, _link, _commit = get(seed_arg)
    return files


def expand(steps, env, seen):
    lines = []
    for step in steps:
        helper = _helper(step)
        if helper is None:
            lines.append(step)
            continue
        if helper in seen:
            raise SeedbankError(f"Integrate steps loop at {helper}")
        folder = materialize(helper)
        lines.extend(expand(integrate_block(folder, env), env, seen | {helper}))
    return lines


def steps(seed_arg, env):
    folder = materialize(seed_arg)
    lines = expand(integrate_block(folder, env), env, seen=set())
    for line in lines:
        print(line)
    return lines
