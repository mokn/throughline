"""
One downloadable snapshot of the archive, for anyone who wants the data
without cloning 43,000 files: every archived market with its current rules
text as gzipped JSON Lines, every recorded rules edit the same way, the
statistics file, and a SHA256SUMS. A weekly job attaches them to a GitHub
Release tagged dump-<date>:

    https://github.com/mokn/throughline/releases

    python3 dump.py [out_dir]          # write the files (default: dist/)
    python3 dump.py --notes [out_dir]  # print the release notes for those files

Nothing here talks to the network.

markets-<date>.jsonl.gz, one object per archived market:
    conditionId, slug, question, url, created_at, first_seen, last_changed,
    last_seen_open (only once the market closed), revisions, sha256, rules
changes-<date>.jsonl.gz, one object per rules edit (feed.record's fields):
    conditionId, slug, question, revision, seen, sha256_before, sha256_after,
    before, after
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

import watch

PREFIX = "dump"


def stamp(runs: Path | None = None) -> str:
    """Date of the last watcher run, from runs.log; today's date if there is none."""
    runs = runs or watch.RUNS
    if runs.exists():
        lines = runs.read_text().splitlines()
        if lines:
            return lines[-1][:10]
    return watch._now().strftime("%Y-%m-%d")


def build(out: Path, date: str | None = None) -> dict:
    """Write the snapshot files into `out`. Returns a manifest with counts and names."""
    date = date or stamp()
    out.mkdir(parents=True, exist_ok=True)
    index = watch._load_index()
    markets = out / f"markets-{date}.jsonl.gz"
    n_markets = 0
    with gzip.open(markets, "wt", encoding="utf-8") as fh:
        for cid in sorted(index):
            rec = index[cid]
            path = watch._rules_path(cid)
            row = {"conditionId": cid, "slug": rec.get("slug"), "question": rec.get("question"),
                   "url": f"https://polymarket.com/market/{rec.get('slug')}",
                   "created_at": rec.get("created_at"), "first_seen": rec.get("first_seen"),
                   "last_changed": rec.get("last_changed")}
            if "last_seen_open" in rec:
                row["last_seen_open"] = rec["last_seen_open"]
            row.update(revisions=rec.get("revisions", 1), sha256=rec.get("sha256"),
                       rules=path.read_text() if path.exists() else "")
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")
            n_markets += 1
    changes = out / f"changes-{date}.jsonl.gz"
    n_changes = 0
    with gzip.open(changes, "wt", encoding="utf-8") as fh:
        if watch.CHANGES.exists():
            for day in sorted(watch.CHANGES.glob("*.json")):
                for r in json.loads(day.read_text()):
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    n_changes += 1
    files = [markets, changes]
    if watch.STATS.exists():
        stats = out / f"stats-{date}.json"
        shutil.copyfile(watch.STATS, stats)
        files.append(stats)
    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{_sha256(f)}  {f.name}\n" for f in files))
    last = None
    if watch.RUNS.exists():
        lines = watch.RUNS.read_text().splitlines()
        last = lines[-1] if lines else None
    return {"date": date, "tag": f"{PREFIX}-{date}", "markets": n_markets, "changes": n_changes,
            "files": [f.name for f in files] + [sums.name],
            "bytes": {f.name: f.stat().st_size for f in files}, "last_run": last}


def notes(manifest: dict) -> str:
    """Release notes in Markdown for one snapshot."""
    sizes = "\n".join(f"| `{name}` | {size / 1e6:.1f} MB |" for name, size in manifest["bytes"].items())
    return f"""Snapshot of the Polymarket resolution-rules archive as of {manifest['date']}.

| | |
|---|---|
| Archived markets | {manifest['markets']:,} |
| Recorded rules edits | {manifest['changes']:,} |
| Last watcher run | `{manifest['last_run'] or 'none'}` |

| File | Size |
|---|---|
{sizes}

`markets-{manifest['date']}.jsonl.gz` has one JSON object per line per archived market
(open markets that live 30 days or longer, plus those that closed since the archive
began on 2026-11-03): conditionId, slug, question, url, created_at, first_seen,
last_changed, last_seen_open (once closed), revisions, sha256 of the rules text, and
the rules text itself. `changes-{manifest['date']}.jsonl.gz` has one object per rules
edit with the text before and after. `SHA256SUMS` covers every file.

Earlier revisions of any market's rules are in git: `python3 history.py <market url>`.
The archive, the client and the code that made this file: https://github.com/mokn/throughline
"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    out = Path(args[0]) if args else watch.ROOT / "dist"
    if "--notes" in argv:
        manifest = json.loads((out / "manifest.json").read_text())
        sys.stdout.write(notes(manifest))
        return 0
    manifest = build(out)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"{out}: {manifest['markets']:,} markets, {manifest['changes']:,} changes, tag {manifest['tag']}")
    for name, size in manifest["bytes"].items():
        print(f"  {name}  {size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
