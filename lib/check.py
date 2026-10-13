import re
from pathlib import Path

from lib.error import SeedbankError
from lib.git import committed_file, dirty
from lib.link import parse_link
from lib.read_seed import KINDS, read_seed
from lib.review import WorkingTree, classify

_LICENSE_NAMES = (
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "COPYING",
)
_BAD_BLANK = re.compile(r"\{\{(?![A-Za-z_][A-Za-z0-9_]*\}\})")


def _frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    found = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        found[key.strip()] = value.strip().strip("\"'")
    return found


def _license_name(folder):
    for name in _LICENSE_NAMES:
        path = Path(folder) / name
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")[:4000]
        if "Permission is hereby granted, free of charge" in text:
            return "MIT"
        if "Apache License" in text and "Version 2.0" in text:
            return "Apache-2.0"
        if "GNU GENERAL PUBLIC LICENSE" in text and "Version 3" in text:
            return "GPL-3.0"
        if "Redistribution and use in source and binary forms" in text:
            return "BSD"
        return "present, not a common one"
    return None


def check(folder=None):
    seed = read_seed(folder)
    folder = seed.folder
    problems = []

    if seed.kind not in KINDS:
        problems.append(
            "set kind to tool, app, game, component, prompt, skill, bundle or standard"
        )
    if not seed.description or len(seed.description) > 160:
        problems.append("make description one sentence, at most 160 characters")

    if seed.kind == "tool" and not (folder / "SKILL.md").is_file():
        problems.append("add SKILL.md saying when to use it and how to run it")

    if seed.kind in ("app", "game") and not committed_file(folder, "ui/index.html"):
        problems.append("commit the built UI at ui/index.html")

    if seed.kind == "component" and not (folder / "ui").exists() and not (folder / "skin").exists():
        problems.append("add ui/ or skin/")

    if seed.kind == "prompt":
        prompt = folder / "PROMPT.md"
        if not prompt.is_file():
            problems.append("add PROMPT.md, with blanks written as {{name}}")
        elif _BAD_BLANK.search(prompt.read_text()):
            problems.append("write every blank in PROMPT.md as {{name}}")

    if seed.kind == "skill":
        skill = folder / "SKILL.md"
        front = _frontmatter(skill.read_text()) if skill.is_file() else {}
        if not front.get("name") or not front.get("description"):
            problems.append("start SKILL.md with name and description")

    if seed.kind == "standard":
        if not (folder / "STANDARD.md").is_file():
            problems.append("add STANDARD.md with the rules in plain words")
        if not (folder / "checks.json").is_file():
            problems.append("add checks.json listing the automated checks")

    if seed.kind == "bundle":
        if not seed.members:
            problems.append("set members to a list of seed links")
        for member in seed.members or []:
            try:
                parse_link(member)
            except SeedbankError:
                problems.append(f"member {member} is not a seed link")
    elif seed.members is not None:
        problems.append("only a bundle has members")

    for dep in seed.dependencies:
        try:
            parse_link(dep)
        except SeedbankError:
            problems.append(f"dependency {dep} is not a seed link")

    if dirty(folder):
        problems.append("commit this folder; the seed is what git has committed")

    license_name = _license_name(folder)
    if license_name is None:
        problems.append("add a LICENSE file (MIT, Apache-2.0, GPL-3.0 or BSD)")

    report = None
    if not problems:
        try:
            report = classify(WorkingTree(folder=folder), vetted=set(), seed=seed)
        except SeedbankError as exc:
            problems.append(str(exc))
        else:
            for item in report.problems:
                if item.level == "fail":
                    where = item.file or "seed"
                    line = f":{item.line}" if item.line else ""
                    problems.append(f"{where}{line}: {item.message}")

    return seed, problems, license_name, report
