"""
An Atom feed of every resolution-rule edit the watcher has seen, each entry a
unified diff of the rules text, so a feed reader or a script can be told when
a market's rules change.

The watcher records each edit as a JSON object in archive/changes/<date>.json
(alongside the human-readable .md) and rebuilds archive/feed.xml from the most
recent records after every run. GitHub Pages serves it (site.py, pages.yml):

    https://mokn.github.io/throughline/feed.xml

Regenerate or inspect by hand (nothing here talks to the network):

    python3 feed.py                 # rebuild archive/feed.xml from archive/changes/*.json
    python3 feed.py --show [N]      # print the N most recent diffs as text (default 10)
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

REPO_URL = "https://github.com/mokn/throughline"
FEED_URL = "https://mokn.github.io/throughline/feed.xml"
FEED_TITLE = "Polymarket resolution-rule changes"
FEED_SUBTITLE = ("Every edit to the resolution rules of an open Polymarket market that "
                 "lives 30 days or longer, as seen by the daily watcher at " + REPO_URL)
MAX_ENTRIES = 200


def record(changes_dir: Path, stamp: str, changed: list) -> int:
    """Append one JSON record per edit to changes_dir/<date>.json. Returns records written."""
    if not changed:
        return 0
    changes_dir.mkdir(parents=True, exist_ok=True)
    path = changes_dir / f"{stamp[:10]}.json"
    records = json.loads(path.read_text()) if path.exists() else []
    for cid, rec, before, after in changed:
        records.append({
            "conditionId": cid,
            "slug": rec.get("slug"),
            "question": rec.get("question"),
            "revision": rec.get("revisions"),
            "seen": stamp,
            "sha256_before": _sha(before),
            "sha256_after": rec.get("sha256"),
            "before": before,
            "after": after,
        })
    path.write_text(json.dumps(records, indent=1, sort_keys=True) + "\n")
    return len(changed)


def load(changes_dir: Path, limit: int = MAX_ENTRIES) -> list[dict]:
    """The most recent `limit` records, newest first."""
    out: list[dict] = []
    if not changes_dir.exists():
        return out
    for path in sorted(changes_dir.glob("*.json"), reverse=True):
        day = json.loads(path.read_text())
        out.extend(reversed(day))
        if len(out) >= limit:
            break
    out.sort(key=lambda r: (r["seen"], r["conditionId"]), reverse=True)
    return out[:limit]


def unified_diff(before: str, after: str, cid: str = "rules") -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{cid} (before)", tofile=f"{cid} (after)", n=2)
    text = "".join(_nl(line) for line in lines)
    return text or "(no textual difference; whitespace or encoding only)\n"


def render(records: list[dict], updated: str) -> str:
    L = ['<?xml version="1.0" encoding="utf-8"?>',
         '<feed xmlns="http://www.w3.org/2005/Atom">',
         f"  <title>{escape(FEED_TITLE)}</title>",
         f"  <subtitle>{escape(FEED_SUBTITLE)}</subtitle>",
         f"  <id>{escape(FEED_URL)}</id>",
         f'  <link rel="self" type="application/atom+xml" href="{escape(FEED_URL)}"/>',
         f'  <link rel="alternate" type="text/html" href="{escape(REPO_URL)}"/>',
         f"  <updated>{escape(updated)}</updated>",
         "  <author><name>throughline rules-watcher</name></author>",
         "  <generator uri=\"" + escape(REPO_URL) + "\">feed.py</generator>"]
    for r in records:
        cid = r["conditionId"]
        title = r.get("question") or r.get("slug") or cid
        market = f"https://polymarket.com/market/{r['slug']}" if r.get("slug") else REPO_URL
        rules = f"{REPO_URL}/blob/main/archive/rules/{_shard(cid)}/{cid}.txt"
        body = (f"Market: {market}\n"
                f"conditionId: {cid}\n"
                f"revision: {r.get('revision')}\n"
                f"seen: {r['seen']}\n"
                f"sha256 before: {r.get('sha256_before')}\n"
                f"sha256 after:  {r.get('sha256_after')}\n\n"
                + unified_diff(r.get("before") or "", r.get("after") or "", cid))
        L += ["  <entry>",
              f"    <title>{escape(title)}</title>",
              f"    <id>tag:throughline.markets,2026:polymarket/rules/{escape(cid)}/{r.get('revision')}</id>",
              f"    <updated>{escape(r['seen'])}</updated>",
              f"    <published>{escape(r['seen'])}</published>",
              f'    <link rel="alternate" type="text/html" href="{escape(market)}"/>',
              f'    <link rel="related" type="text/plain" href="{escape(rules)}"/>',
              f"    <summary>Revision {r.get('revision')} of the resolution rules, seen {escape(r['seen'])}.</summary>",
              f'    <content type="text">{escape(body)}</content>',
              "  </entry>"]
    L += ["</feed>", ""]
    return "\n".join(L)


def build(changes_dir: Path, out: Path, last_run: str | None = None) -> int:
    """Write the Atom feed from the recorded changes. Returns the entry count."""
    records = load(changes_dir)
    stamps = [r["seen"] for r in records] + ([last_run] if last_run else [])
    updated = max(stamps) if stamps else "1970-01-01T00:00:00Z"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(records, updated))
    return len(records)


def _sha(text: str) -> str:
    from rules import rules_hash
    return rules_hash(text)


def _shard(cid: str) -> str:
    import watch
    return watch._shard(cid)


def _nl(line: str) -> str:
    return line if line.endswith("\n") else line + "\n"


def main(argv: list[str]) -> int:
    import watch
    changes_dir, out = watch.CHANGES, watch.ARCHIVE / "feed.xml"
    if "--show" in argv:
        i = argv.index("--show")
        n = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 10
        records = load(changes_dir, n)
        if not records:
            print("no rule changes recorded yet")
            return 0
        for r in records:
            print(f"== {r.get('question') or r['conditionId']}  (revision {r.get('revision')}, seen {r['seen']})")
            print(unified_diff(r.get("before") or "", r.get("after") or "", r["conditionId"]))
        return 0
    last = None
    if watch.RUNS.exists():
        lines = watch.RUNS.read_text().splitlines()
        if lines:
            last = lines[-1].split(" ", 1)[0]
    n = build(changes_dir, out, last)
    print(f"{out}: {n} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
