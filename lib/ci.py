"""Catalog CI: `seedbank review --ci <row>`.

Validates a row against its pinned commit, runs the classifier, prints a
comment body on stdout, and exits non-zero if the stored lane disagrees or
there's a fail-level problem.

Scanners (clamav / gitleaks) run from the workflow itself; this module does
the shape and lane checks only.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from lib.catalog import ROW_FIELDS, _row_path
from lib.error import SeedbankError
from lib.fetch import _bare, checkout, tree_of
from lib.link import Link, parse_link
from lib.read_seed import read_seed
from lib.review import CommitSource, classify

CI_MARKER = "<!-- seedbank-review -->"


def validate_row(row, row_path):
    """Return (link, row). Raises for shape, owner, or missing fields."""
    unknown = [k for k in row if k not in ROW_FIELDS]
    if unknown:
        raise SeedbankError(f"row has unknown field {unknown[0]}")
    for key in ("name", "link", "commit", "tree"):
        if not row.get(key):
            raise SeedbankError(f"row missing {key}")
    expected = _row_path(row["name"])
    actual = str(row_path).replace(os.sep, "/")
    if actual != expected:
        raise SeedbankError(f"row path is {actual}, should be {expected}")
    name_owner = row["name"].split("/", 1)[0]
    link = parse_link(row["link"])
    if not isinstance(link, Link):
        raise SeedbankError(f"link must be host/owner/repo, got {row['link']}")
    if link.owner != name_owner:
        raise SeedbankError(f"name owner {name_owner} != link owner {link.owner}")
    if link.ref:
        raise SeedbankError("link must not include @ref; the row's commit is the ref")
    return link


def _is_blocked(author, blocked_path):
    if not author or not blocked_path.is_file():
        return False
    author = author.lower()
    for line in blocked_path.read_text().splitlines():
        name = line.split("#", 1)[0].strip()
        if name.lower() == author:
            return True
    return False


def _open_pr_count(repo_slug, author):
    """Count open PRs by `author` against `repo_slug` (used for rate limits)."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", repo_slug,
         "--author", author, "--state", "open",
         "--json", "number"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    try:
        return len(json.loads(proc.stdout))
    except json.JSONDecodeError:
        return 0


def review_ci(row_path_arg,
              previous_row=None,
              author=None,
              catalog_root=".",
              existing_row_bytes=None):
    """Classify a row's pinned commit; return a dict for the workflow.

    row_path_arg      -- path to the PR's row file inside the catalog clone
    previous_row      -- dict for an update (the base-branch row), or None
    author            -- GitHub login of the PR author, or None
    catalog_root      -- the catalog clone root
    existing_row_bytes-- optional override for the row content, as JSON bytes
    """
    path = Path(row_path_arg)
    if existing_row_bytes is not None:
        row = json.loads(existing_row_bytes.decode())
    else:
        row = json.loads(path.read_text())

    rel_from_root = str(path).replace(os.sep, "/")
    link = validate_row(row, rel_from_root)

    # Fetch the pinned commit and verify its tree hash.
    _bare_dir = _bare(link)
    real_tree = tree_of(link, row["commit"])
    if real_tree != row["tree"]:
        raise SeedbankError(
            f"tree mismatch: {row['commit'][:7]} has tree {real_tree[:7]}, "
            f"row says {row['tree'][:7]}"
        )

    # Delisted flips can only come from etienne (checked by the workflow via CODEOWNERS,
    # repeated here as belt + braces).
    prev_delisted = (previous_row or {}).get("delisted")
    new_delisted = row.get("delisted")
    if new_delisted != prev_delisted and (author or "") != "etiennedoidic":
        raise SeedbankError("only etiennedoidic can change `delisted`")

    files = checkout(link, row["commit"], link.folder)
    seed = read_seed(files, name_hint=(link.folder.rsplit("/", 1)[-1] if link.folder else link.repo))

    previous = None
    if previous_row and previous_row.get("commit"):
        previous = {"commit": previous_row["commit"]}
    report = classify(
        CommitSource(bare=_bare_dir, commit=row["commit"], folder=link.folder or ""),
        vetted=set(),
        seed=seed,
        previous=previous,
    )

    stored_lane = row.get("lane") or ""
    computed = report.lane

    disagreement = None
    if stored_lane and computed and stored_lane != computed:
        disagreement = f"row says lane {stored_lane}, CI computes lane {computed}"

    auto_merge = computed in ("A", "B") and not report.failing() and disagreement is None

    body = _comment(row, link, seed, report, disagreement)
    return {
        "lane": computed,
        "stored_lane": stored_lane,
        "disagreement": disagreement,
        "body": body,
        "auto_merge": auto_merge,
        "report": report,
    }


def _lane_label(lane, report):
    if report.failing():
        return "needs changes"
    label = {
        "A": "lane A, listed automatically (content only)",
        "B": "lane B, listed automatically (locked UI, no network)",
        "C": "lane C, short review",
        "D": "lane D, full review",
    }.get(lane, "no lane")
    return label


def _comment(row, link, seed, report, disagreement):
    short = row["commit"][:7]
    lines = [CI_MARKER]
    lines.append(f"**seedbank review:** {_lane_label(report.lane, report)}")
    lines.append(f"`{row['name']}` @ `{short}`")
    lines.append("")
    perms = ", ".join(report.effective_permissions) or "nothing"
    needs = ", ".join(seed.requires or []) or "nothing"
    lines.append(f"- asks: `{perms}`")
    lines.append(f"- needs: `{needs}`")
    new_or_changed = [f for f in report.files if f.status in ("new", "changed")]
    lines.append(
        f"- new or changed: {len(new_or_changed)} file(s), "
        f"{report.new_script_lines} script line(s)"
    )
    if report.skipped_trees:
        lines.append(f"- vetted folders skipped: {len(report.skipped_trees)}")
    if disagreement:
        lines.append(f"- **fail** classifier: {disagreement}")
    for p in report.problems:
        where = p.file or "seed"
        line = f":{p.line}" if p.line else ""
        lines.append(f"- **{p.level}** {p.check} `{where}{line}`: {p.message}")
    return "\n".join(lines)


def main_cli(args):
    """Entry point for `seedbank review --ci <row>`."""
    author = os.environ.get("SEEDBANK_PR_AUTHOR") or None
    previous_json = os.environ.get("SEEDBANK_PREV_ROW")
    previous_row = None
    if previous_json:
        try:
            previous_row = json.loads(previous_json)
        except json.JSONDecodeError:
            previous_row = None
    row_path = args[0] if args else ""
    if not row_path:
        raise SeedbankError("seedbank review --ci <row file>")
    result = review_ci(row_path, previous_row=previous_row, author=author)
    sys.stdout.write(result["body"] + "\n")
    # Machine-readable summary on stderr for the workflow to parse.
    sys.stderr.write(json.dumps({
        "lane": result["lane"],
        "stored_lane": result["stored_lane"],
        "disagreement": result["disagreement"],
        "auto_merge": result["auto_merge"],
    }) + "\n")
    failed = result["disagreement"] or result["report"].failing()
    if failed:
        raise SeedbankError("review failed")
