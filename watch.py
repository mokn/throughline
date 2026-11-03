"""
Snapshot every open Polymarket market's resolution rules and record what changed.

Runs daily in GitHub Actions. Each run:

1. Enumerates every open market (partitioned, so it does not stop at the
   offset wall).
2. Hashes each market's ``description``.
3. Compares against archive/index.json from the previous run.
4. Writes archive/rules/<conditionId>.txt for new or changed rules, and
   archive/changes/<date>.md listing every change with before/after text.
5. Appends one line to archive/runs.log whether or not anything changed, so a
   gap in the log is visible as a gap.

It also flags markets whose ``createdAt`` is well before the date this archive
first saw them. If enumeration is complete, every market should be seen within
a day of creation; a late first sighting means a market was missed, and the
archive says so rather than quietly backfilling.

    python3 watch.py            # run once, write archive/
    python3 watch.py --dry-run  # enumerate and report, write nothing
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import gamma
from rules import rules_hash

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "archive"
INDEX = ARCHIVE / "index.json"
RULES = ARCHIVE / "rules"
CHANGES = ARCHIVE / "changes"
RUNS = ARCHIVE / "runs.log"
LATE_SIGHTING_DAYS = 2   # first_seen - createdAt beyond this is an enumeration gap


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_index() -> dict:
    if INDEX.exists():
        return json.loads(INDEX.read_text())
    return {}


def _days_between(a: str | None, b: str) -> float | None:
    ea, eb = gamma._epoch(a or ""), gamma._epoch(b)
    if ea is None or eb is None:
        return None
    return (eb - ea) / 86400


def run(dry_run: bool = False) -> int:
    started = _now()
    today = started.strftime("%Y-%m-%d")
    stamp = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    g = gamma.Gamma()
    try:
        markets = g.markets_all(closed=False)
    except gamma.TruncatedEnumeration as e:
        _log(f"{stamp} FAILED truncated requests={g.requests} {e}", dry_run)
        return 1
    except gamma.GammaError as e:
        _log(f"{stamp} FAILED gamma requests={g.requests} {e}", dry_run)
        return 1

    index = _load_index()
    new, changed, late = [], [], []
    for cid, m in sorted(markets.items()):
        h = rules_hash(m.get("description"))
        prev = index.get(cid)
        if prev is None:
            index[cid] = {
                "slug": m.get("slug"),
                "question": m.get("question"),
                "sha256": h,
                "created_at": m.get("createdAt"),
                "first_seen": stamp,
                "last_changed": stamp,
                "revisions": 1,
            }
            new.append(cid)
            age = _days_between(m.get("createdAt"), stamp)
            if index and age is not None and age > LATE_SIGHTING_DAYS and len(index) > len(new):
                late.append((cid, age))
            if not dry_run:
                _write_rules(cid, m.get("description"))
        elif prev["sha256"] != h:
            before = (RULES / f"{cid}.txt").read_text() if (RULES / f"{cid}.txt").exists() else ""
            changed.append((cid, prev, before, m.get("description") or ""))
            prev["sha256"] = h
            prev["last_changed"] = stamp
            prev["revisions"] = prev.get("revisions", 1) + 1
            prev["question"] = m.get("question")
            if not dry_run:
                _write_rules(cid, m.get("description"))

    seen_now = set(markets)
    for cid, rec in index.items():
        if cid in seen_now:
            rec.pop("last_seen_open", None)
        elif "last_seen_open" not in rec:
            rec["last_seen_open"] = stamp   # resolved, closed, or dropped out of the open set

    line = (f"{stamp} ok markets={len(markets)} requests={g.requests} "
            f"new={len(new)} changed={len(changed)} late_first_sighting={len(late)} "
            f"seconds={int((_now() - started).total_seconds())}")
    if dry_run:
        print(line)
        for cid, prev, before, after in changed:
            print(f"  changed {cid} {prev['slug']}")
        return 0

    ARCHIVE.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
    if changed or late:
        CHANGES.mkdir(exist_ok=True)
        _write_changes(today, stamp, changed, late, index)
    _log(line, dry_run)
    print(line)
    return 0


def _write_rules(cid: str, description: str | None) -> None:
    RULES.mkdir(parents=True, exist_ok=True)
    (RULES / f"{cid}.txt").write_text(description or "")


def _write_changes(today: str, stamp: str, changed: list, late: list, index: dict) -> None:
    path = CHANGES / f"{today}.md"
    parts = [] if not path.exists() else [path.read_text()]
    parts.append(f"## Run {stamp}\n")
    for cid, prev, before, after in changed:
        parts.append(f"### {prev['question']}\n")
        parts.append(f"- market: https://polymarket.com/market/{prev['slug']}")
        parts.append(f"- conditionId: `{cid}`")
        parts.append(f"- revision: {prev['revisions']}")
        parts.append(f"- sha256: `{prev['sha256']}`\n")
        parts.append("**Before**\n")
        parts.append("```\n" + before + "\n```\n")
        parts.append("**After**\n")
        parts.append("```\n" + after + "\n```\n")
    if late:
        parts.append("### Late first sightings (possible enumeration gap)\n")
        parts.append("These markets were created more than "
                     f"{LATE_SIGHTING_DAYS} days before this archive first saw them. "
                     "Either enumeration missed them earlier or they were not open until now.\n")
        for cid, age in late:
            rec = index[cid]
            parts.append(f"- `{cid}` {rec['slug']} created {rec['created_at']} ({age:.0f} days ago)")
        parts.append("")
    path.write_text("\n".join(parts))


def _log(line: str, dry_run: bool) -> None:
    if dry_run:
        print(line)
        return
    ARCHIVE.mkdir(exist_ok=True)
    with RUNS.open("a") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    sys.exit(run(dry_run="--dry-run" in sys.argv[1:]))
