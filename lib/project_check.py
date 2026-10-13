"""Project-level `check`: walk a repo that has `.seedbank/`."""

import json
from pathlib import Path

from lib.error import SeedbankError
from lib.ledger import bank_path, read_bank, read_vetted
from lib.review import (
    File,
    _list_working,
    _classify_ext,
    _looks_binary,
    _under_skipped,
)


def project_root(folder):
    folder = Path(folder).resolve()
    for parent in (folder, *folder.parents):
        if (parent / ".seedbank" / "bank.json").is_file():
            return parent
    return None


def _skipped_from_bank(data):
    return [item.rstrip("/") for item in (data.get("skip") or [])]


def project_check(folder=".", strict=False, brief=False, as_json=False):
    """Return a report dict; print the map unless as_json=True."""
    root = project_root(folder)
    if root is None:
        raise SeedbankError("no .seedbank/bank.json up from here; run `seedbank init`")
    bank = read_bank(root)
    reviewers = bank.get("reviewers") or {}
    entries, vetted, ledger_problems = read_vetted(root, reviewers)

    skip = _skipped_from_bank(bank)

    _root_tree, files, trees = _list_working(root)

    # Build vetted sets including sha-to-path for status transitions.
    vetted_blob_shas = {e.sha for e in entries if e.kind == "blob"}
    vetted_tree_shas = {e.sha for e in entries if e.kind == "tree"}
    path_to_vetted_blob = {}
    for e in entries:
        if e.kind == "blob":
            path_to_vetted_blob[e.path] = e.sha

    # Decide skipped tree-prefixes: either in bank.skip or a vetted tree.
    skipped_prefixes = []
    for seg in skip:
        skipped_prefixes.append(seg)
    skipped_trees = []
    for path, sha in trees:
        if path and sha in vetted_tree_shas:
            skipped_prefixes.append(path)
            skipped_trees.append((path, sha))

    vetted_files = []
    changed_files = []
    new_files = []
    automatic_count = 0
    manual_count = 0

    for path, sha, size, mode in files:
        if path.startswith(".seedbank/"):
            continue
        if _under_skipped(path, skipped_prefixes):
            vetted_files.append((path, sha))
            continue
        kind = _classify_ext(path, mode)
        if sha in vetted_blob_shas:
            vetted_files.append((path, sha))
            continue
        if path in path_to_vetted_blob and path_to_vetted_blob[path] != sha:
            changed_files.append((path, sha, path_to_vetted_blob[path]))
            manual_count += 1
            continue
        new_files.append((path, sha, kind))
        if kind in ("content", "svg"):
            automatic_count += 1
        else:
            manual_count += 1

    total = len(vetted_files) + len(changed_files) + len(new_files)
    result = {
        "root": str(root),
        "totals": {
            "files": total,
            "vetted": len(vetted_files),
            "changed": len(changed_files),
            "new": len(new_files),
        },
        "changed": [{"path": p, "sha": s, "was": o} for p, s, o in changed_files],
        "new": [{"path": p, "sha": s, "kind": k} for p, s, k in new_files],
        "standards": bank.get("standards") or [],
        "trust": bank.get("trust") or [],
        "ledger_problems": ledger_problems,
    }

    if as_json:
        print(json.dumps(result, indent=2))
    elif brief:
        _print_brief(result, bank)
    else:
        _print_map(result)

    if strict:
        if changed_files:
            raise SeedbankError(f"{len(changed_files)} vetted file(s) have changed")
        remaining_manual = manual_count
        if remaining_manual:
            raise SeedbankError(f"{remaining_manual} file(s) need review")
        if ledger_problems:
            raise SeedbankError("ledger has unsigned or invalid blocks")

    return result


def _print_map(r):
    totals = r["totals"]
    print(f"  grimoire  {totals['files']} files")
    print(f"    vetted    {totals['vetted']:>3}   matches something you or a bank you trust approved")
    marker = "! " if totals["changed"] else "  "
    print(f"    changed   {marker}{totals['changed']:>3}   was vetted, now different")
    print(f"    new       {totals['new']:>3}   never reviewed")
    if r["standards"]:
        print(f"  standards  {', '.join(r['standards'])}")
    else:
        print("  standards  none yet")
    for problem in r["ledger_problems"]:
        print(f"  warn: {problem}")
    if totals["changed"] or totals["new"]:
        n = totals["changed"] + totals["new"]
        first = "the changed ones" if totals["changed"] else "the new ones"
        print(f"  next: seedbank review   ({n} file(s), starting with {first})")


def _print_brief(r, bank):
    totals = r["totals"]
    print(f"# seedbank brief: {totals['vetted']} vetted, {totals['changed']} changed, {totals['new']} new")
    if bank.get("standards"):
        print("## standards")
        for s in bank["standards"]:
            print(f"- {s}")
    print("## rule")
    print("- Do not edit vetted files unless necessary. If you must, say which and why.")
