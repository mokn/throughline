"""
Assemble the small static site that GitHub Pages serves: the Atom feed with a
proper content type, and an index page that says what the feed is and when
the watcher last ran. Nothing else goes to Pages; the archive itself stays in
git, where it is 43,000 files that a Pages build should never have to touch.

    python3 site.py [out_dir]     # default: site/
"""
from __future__ import annotations

import shutil
import sys
from html import escape
from pathlib import Path

import feed
import watch

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket resolution-rule changes</title>
<link rel="alternate" type="application/atom+xml" title="{title}" href="feed.xml">
<style>
  body {{ max-width: 42rem; margin: 3rem auto; padding: 0 1rem; font: 16px/1.5 system-ui, sans-serif; color: #1a1a1a; background: #fff; }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.95em; }}
  pre {{ background: #f4f4f4; padding: 0.75rem 1rem; overflow-x: auto; }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #e6e6e6; background: #111; }} pre {{ background: #1e1e1e; }} }}
</style>
</head>
<body>
<h1>Polymarket resolution-rule changes</h1>
<p>An Atom feed of every edit to the resolution rules of an open Polymarket market
that lives 30 days or longer. Each entry is one edit: the market, its revision
number, the hash before and after, and a unified diff of the rules text.</p>
<pre>{feed_url}</pre>
<p>Entries in the feed now: <strong>{entries}</strong>. Last watcher run: <code>{last_run}</code>.
The feed's <code>updated</code> stamp moves with every run, so a feed that stops moving
means the watcher stopped, not that nothing changed.</p>
<p>The archive, the client that enumerates every open market, the statistics and the
code behind this page are at <a href="{repo}">{repo}</a>. Not affiliated with Polymarket.
Published by <a href="https://throughline.markets">Throughline</a>, whose operator also
trades on Polymarket; nothing here is a position or a recommendation.</p>
</body>
</html>
"""


def build(out: Path, feed_path: Path | None = None, runs: Path | None = None) -> Path:
    feed_path = feed_path or watch.FEED
    runs = runs or watch.RUNS
    out.mkdir(parents=True, exist_ok=True)
    if feed_path.exists():
        shutil.copyfile(feed_path, out / "feed.xml")
        entries = feed_path.read_text().count("<entry>")
    else:
        (out / "feed.xml").write_text(feed.render([], "1970-01-01T00:00:00Z"))
        entries = 0
    last = "never"
    if runs.exists():
        lines = runs.read_text().splitlines()
        if lines:
            last = lines[-1]
    (out / "index.html").write_text(PAGE.format(
        title=escape(feed.FEED_TITLE), feed_url=escape(feed.FEED_URL), repo=escape(feed.REPO_URL),
        entries=entries, last_run=escape(last)))
    (out / ".nojekyll").write_text("")
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else watch.ROOT / "site"
    print(build(target))
