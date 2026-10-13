"""seedbank review <pr> - pull a catalog PR to disk and act on it.

Fetches the pinned commit with `git archive` (no checkout hooks, nothing
executed), writes `CHANGES.diff` since the previous history entry, opens the
folder in $SEEDBANK_REVIEW_EDITOR, and offers approve / reject / changes
through `gh`.
"""

import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

from lib.catalog import PUBLIC_URL, _github_slug, open_catalog, PUBLIC_BRANCH
from lib.ci import review_ci
from lib.error import SeedbankError
from lib.fetch import _bare, _archive_bytes
from lib.link import parse_link
from lib.store import cache_root

PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _parse_pr_arg(arg):
    """Return (repo_slug, pr_number) from a number, a URL, or an owner/repo#n."""
    arg = arg.strip()
    match = PR_URL.search(arg)
    if match:
        return f"{match.group(1)}/{match.group(2)}", int(match.group(3))
    if arg.isdigit():
        return _github_slug(PUBLIC_URL), int(arg)
    if "#" in arg:
        slug, number = arg.rsplit("#", 1)
        return slug, int(number)
    raise SeedbankError(f"could not read PR {arg!r}; give a URL, a number, or owner/repo#n")


def _review_dir(pr):
    path = cache_root() / "review" / str(pr)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _gh_json(args):
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SeedbankError((proc.stderr or proc.stdout).strip() or "gh failed")
    if not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SeedbankError(f"could not parse gh output ({exc})") from exc


def _read_row_at(slug, path, ref):
    """Fetch a row file as bytes at a specific ref."""
    proc = subprocess.run(
        [
            "gh", "api",
            "--header", "Accept: application/vnd.github.raw",
            f"repos/{slug}/contents/{path}?ref={ref}",
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _base_row(slug, path, base_ref):
    data = _read_row_at(slug, path, base_ref)
    if not data:
        return None
    try:
        return json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_archive(link, commit, dest):
    data = _archive_bytes(link, commit, link.folder)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
    # Flatten the folder prefix like fetch.checkout does.
    if link.folder:
        nested = dest.joinpath(*link.folder.split("/"))
        if nested.is_dir():
            for child in list(nested.iterdir()):
                child.rename(dest / child.name)
            parent = nested
            while parent != dest:
                parent.rmdir()
                parent = parent.parent


def _write_changes_diff(link, head_row, prev_commit, out_file):
    if not prev_commit:
        out_file.write_text("")
        return
    bare = _bare(link)
    args = [
        "git", "-C", str(bare), "diff",
        prev_commit, head_row["commit"],
    ]
    if link.folder:
        args.extend(["--", link.folder])
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode == 0:
        out_file.write_text(proc.stdout)
    else:
        out_file.write_text(f"(diff failed: {proc.stderr})")


def _open_editor(folder):
    cmd = os.environ.get("SEEDBANK_REVIEW_EDITOR", "code --new-window")
    parts = shlex.split(cmd) + [str(folder)]
    try:
        subprocess.Popen(parts)
    except FileNotFoundError:
        print(f"  could not run {parts[0]}; set SEEDBANK_REVIEW_EDITOR to another editor")


def _print_report(result):
    print(result["body"])
    print()
    print(f"lane:          {result['lane']}")
    if result["stored_lane"]:
        print(f"stored_lane:   {result['stored_lane']}")
    if result["disagreement"]:
        print(f"disagreement:  {result['disagreement']}")
    print(f"auto_merge:    {result['auto_merge']}")


def review_pr(arg, approve=False, reject_body=None, changes_body=None):
    slug, pr_number = _parse_pr_arg(arg)
    pr = _gh_json([
        "gh", "pr", "view", str(pr_number),
        "--repo", slug,
        "--json", "number,headRefOid,baseRefName,files,author,state",
    ])
    files = [item["path"] for item in pr.get("files", [])]
    if len(files) != 1:
        raise SeedbankError(f"PR changes {len(files)} file(s); must be exactly one row")
    row_path = files[0]
    head = pr["headRefOid"]
    base = pr.get("baseRefName") or "catalog"

    head_bytes = _read_row_at(slug, row_path, head)
    if head_bytes is None:
        raise SeedbankError(f"could not read {row_path} at {head[:7]}")
    try:
        head_row = json.loads(head_bytes.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeedbankError(f"PR row is not valid JSON ({exc})") from exc
    prev_row = _base_row(slug, row_path, base) or {}
    prev_commit = prev_row.get("commit")

    link = parse_link(head_row["link"])

    dest = _review_dir(pr_number)
    print(f"  downloading {head_row['name']} @ {head_row['commit'][:7]} to {dest}")
    _write_archive(link, head_row["commit"], dest)
    _write_changes_diff(link, head_row, prev_commit, dest.parent / f"{pr_number}.diff")
    diff_path = dest.parent / f"{pr_number}.diff"

    result = review_ci(
        row_path,
        previous_row=prev_row or None,
        author=(pr.get("author") or {}).get("login"),
        existing_row_bytes=head_bytes,
    )
    _print_report(result)
    print(f"  files    {dest}")
    print(f"  diff     {diff_path}")

    if approve:
        print("\n  approving and merging...")
        try:
            _run(["gh", "pr", "review", str(pr_number), "--repo", slug, "--approve"])
        except SeedbankError as exc:
            # GitHub refuses self-approval ("Can not approve your own pull request").
            # In the public catalog etienne reviews other people's PRs, so this is
            # harmless; just print and keep going.
            print(f"  (skip --approve: {exc})")
        _run(["gh", "pr", "merge", str(pr_number), "--repo", slug, "--squash", "--delete-branch"])
        print("  merged.")
    elif reject_body is not None:
        print("\n  closing with a comment...")
        _run(["gh", "pr", "close", str(pr_number), "--repo", slug, "--comment", reject_body])
        print("  closed.")
    elif changes_body is not None:
        print("\n  requesting changes...")
        _run([
            "gh", "pr", "review", str(pr_number),
            "--repo", slug,
            "--request-changes",
            "--body", changes_body,
        ])
        print("  requested changes.")
    else:
        _open_editor(dest)

    return result


def _run(args):
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SeedbankError((proc.stderr or proc.stdout).strip() or f"{args[0]} failed")
    if proc.stdout.strip():
        print(proc.stdout.strip())


def review_row_file(row_path_arg):
    """`seedbank review <row file>` for non-GitHub catalogs."""
    path = Path(row_path_arg).resolve()
    if not path.is_file():
        raise SeedbankError(f"no row at {path}")
    try:
        rel = path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        rel = path.as_posix()
    result = review_ci(rel)
    _print_report(result)
    return result
