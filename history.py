"""
Every archived revision of a market's resolution rules, from this repository's
git history.

Gamma keeps no revision history. The watcher commits a market's rules text
every time its hash changes, so git holds the history: one commit per revision,
each dated by the run that saw it.

    python3 history.py <market url | slug | conditionId>
    python3 history.py <market ...> --verify <sha256>

Without ``--verify``: prints the index record and each archived revision with
the date it was first seen, its hash, and its text. With ``--verify``: prints
when the given hash was the live text, or says it was never archived. Exit
status is 0 when the hash was seen, 1 when it was not.

Nothing here talks to the network: if the market is not in the archive, the
answer is "not archived" (the market may be younger than the watcher's minimum
lifetime, closed before the archive started, or simply not seen yet).
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import gamma
import watch

ROOT = watch.ROOT


def _git(*args: str, root: Path | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=root or ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


def find(ref: str, index: dict | None = None) -> tuple[str, dict] | None:
    """Resolve a URL, slug or condition ID against the archive index."""
    index = watch._load_index() if index is None else index
    if ref in index:
        return ref, index[ref]
    slug = gamma.slug_of(ref)
    for cid, rec in index.items():
        if rec.get("slug") == slug:
            return cid, rec
    return None


def revisions(cid: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Oldest first: one entry per commit that touched the market's rules file."""
    root = root or ROOT
    rel = watch._rules_path(cid).relative_to(root).as_posix()
    has_commits = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root,
                                 capture_output=True).returncode == 0
    log = _git("log", "--reverse", "--format=%H %cI", "--", rel, root=root).split() if has_commits else []
    out: list[dict[str, Any]] = []
    for commit, date in zip(log[0::2], log[1::2]):
        text = _git("show", f"{commit}:{rel}", root=root)
        out.append({"commit": commit[:12], "date": date, "sha256": sha(text), "text": text})
    # the working tree may hold an uncommitted newer revision (a run in progress)
    path = root / rel
    if path.exists():
        text = path.read_text()
        if not out or out[-1]["sha256"] != sha(text):
            out.append({"commit": "(working tree)", "date": "", "sha256": sha(text), "text": text})
    return out


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify(cid: str, digest: str, root: Path | None = None) -> list[dict[str, Any]]:
    """The revisions whose text hashes to ``digest``; empty if never archived."""
    digest = digest.strip().lower()
    return [r for r in revisions(cid, root or ROOT) if r["sha256"] == digest]


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--verify"]
    want = None
    if "--verify" in argv:
        i = argv.index("--verify")
        if i + 1 >= len(argv):
            print(__doc__)
            return 2
        want = argv[i + 1]
        args = [a for a in argv[1:] if a not in ("--verify", want)]
    if len(args) != 1:
        print(__doc__)
        return 2
    hit = find(args[0])
    if hit is None:
        print(f"not archived: {args[0]}")
        return 1
    cid, rec = hit
    if want is not None:
        seen = verify(cid, want)
        if not seen:
            print(f"never archived for {cid}: {want}")
            return 1
        for r in seen:
            print(f"{r['sha256']}  seen {r['date'] or 'working tree'}  commit {r['commit']}")
        return 0
    print(f"conditionId   {cid}")
    print(f"slug          {rec.get('slug')}")
    print(f"question      {rec.get('question')}")
    print(f"first_seen    {rec.get('first_seen')}")
    print(f"last_changed  {rec.get('last_changed')}")
    print(f"revisions     {rec.get('revisions')}")
    for n, r in enumerate(revisions(cid), 1):
        print()
        print(f"--- revision {n}  {r['date'] or 'working tree'}  {r['commit']}  sha256 {r['sha256']}")
        print(r["text"] or "(empty description)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
