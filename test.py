"""
Tests. Offline tests use a fake transport and run in under a second:

    python3 test.py

Live tests hit the real Gamma API and re-measure every claim in GOTCHAS.md.
If one fails, the API changed and the doc is stale:

    python3 test.py --live
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
import urllib.parse
from unittest import mock

import gamma
from rules import rules_hash, fingerprint

LIVE = "--live" in sys.argv
if LIVE:
    sys.argv.remove("--live")


# -- fake Gamma ---------------------------------------------------------------

class FakeGamma:
    """Reproduces the measured behaviours: limit clamp, offset wall, inclusive
    date filters, unknown params ignored."""

    def __init__(self, markets: list[dict]) -> None:
        self.markets = markets
        self.calls = 0

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls += 1
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        path = urllib.parse.urlsplit(url).path
        if path == "/markets" and "slug" in q:
            rows = [m for m in self.markets if m["slug"] == q["slug"]]
            return 200, json.dumps(rows[:1]).encode()
        if path == "/markets" and "condition_ids" in q:
            rows = [m for m in self.markets if m["conditionId"] == q["condition_ids"]]
            return 200, json.dumps(rows[:1]).encode()
        if path == "/events":
            rows = [e for e in getattr(self, "events", []) if e["slug"] == q.get("slug")]
            return 200, json.dumps(rows[:1]).encode()
        offset = int(q.get("offset", 0))
        limit = min(int(q.get("limit", 100)), 100)      # measured clamp
        if offset > gamma.MAX_OFFSET:
            return 422, b'{"error":"offset too large"}'
        rows = self.markets
        if q.get("closed") == "false":
            rows = [m for m in rows if not m["closed"]]
        for axis in ("end_date", "start_date"):
            field = "endDate" if axis == "end_date" else "startDate"
            lo, hi = q.get(f"{axis}_min"), q.get(f"{axis}_max")
            if lo and hi and lo >= hi:    # the real API: equal bounds are "invalid time range"
                return 422, b'{"type":"validation error","error":"error validating query argument \"end_date\": invalid time range"}'
            if lo:
                rows = [m for m in rows if m.get(field) and m[field] >= lo]
            if hi:
                rows = [m for m in rows if m.get(field) and m[field] <= hi]
        return 200, json.dumps(rows[offset:offset + limit]).encode()


def _mk(i: int, end: str, start: str = "2025-06-01T00:00:00Z", closed: bool = False) -> dict:
    return {"id": str(i), "conditionId": f"0x{i:064x}", "slug": f"m-{i}", "question": f"Q{i}",
            "description": f"rules {i}", "endDate": end, "startDate": start, "closed": closed,
            "createdAt": start, "outcomes": '["Yes", "No"]', "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["1", "2"]', "umaResolutionStatuses": "[]"}


def _spread(n: int, year_from: int = 2026, year_to: int = 2029) -> list[dict]:
    out = []
    for i in range(n):
        y = year_from + (i * (year_to - year_from)) // n
        mo = 1 + (i * 12 // n) % 12
        out.append(_mk(i, f"{y}-{mo:02d}-15T12:00:00Z"))
    return out


# -- offline ------------------------------------------------------------------

if "--live" not in sys.argv:
    # Offline tests drive a fake transport; the token bucket would only slow
    # them down. Tests that exercise the bucket pass rate/clock/sleep explicitly.
    _real_init = gamma.Gamma.__init__

    def _fast_init(self, *a, **k):
        k.setdefault("rate", float("inf"))
        k.setdefault("sleep", lambda s: None)
        _real_init(self, *a, **k)

    gamma.Gamma.__init__ = _fast_init

class SlugTests(unittest.TestCase):
    def test_forms(self) -> None:
        for s in ("abc", "/event/abc", "https://polymarket.com/event/abc?tid=1",
                  "https://polymarket.com/market/abc/", "markets/abc"):
            self.assertEqual(gamma.slug_of(s), "abc", s)
        self.assertEqual(gamma.slug_of(""), "")
        self.assertEqual(gamma.slug_of("https://polymarket.com/"), "")


class NormalizeTests(unittest.TestCase):
    def test_decodes_all_four_string_json_fields(self) -> None:
        m = gamma.normalize(_mk(1, "2027-01-01T00:00:00Z"))
        self.assertEqual(m["outcomes"], ["Yes", "No"])
        self.assertEqual(m["outcomePrices"], ["0.5", "0.5"])
        self.assertEqual(m["clobTokenIds"], ["1", "2"])
        self.assertEqual(m["umaResolutionStatuses"], [])

    def test_leaves_invalid_json_untouched_and_does_not_mutate(self) -> None:
        raw = _mk(1, "2027-01-01T00:00:00Z")
        raw["outcomes"] = "not json"
        m = gamma.normalize(raw)
        self.assertEqual(m["outcomes"], "not json")
        self.assertIsInstance(raw["outcomePrices"], str)

    def test_already_decoded_passthrough(self) -> None:
        raw = _mk(1, "2027-01-01T00:00:00Z")
        raw["outcomes"] = ["Yes", "No"]
        self.assertEqual(gamma.normalize(raw)["outcomes"], ["Yes", "No"])


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sleep = gamma.time.sleep
        gamma.time.sleep = lambda s: None

    def tearDown(self) -> None:
        gamma.time.sleep = self._sleep

    def test_retries_then_raises(self) -> None:
        calls = []
        def fetch(url: str) -> tuple[int, bytes]:
            calls.append(url)
            return 503, b"down"
        g = gamma.Gamma(fetch=fetch, retries=2)
        with self.assertRaises(gamma.GammaError) as cm:
            g._get("/markets")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(len(calls), 3)

    def test_network_error_retried_then_raises_status_0(self) -> None:
        calls = []
        def fetch(url: str) -> tuple[int, bytes]:
            calls.append(url)
            return 0, b"The read operation timed out"
        with self.assertRaises(gamma.GammaError) as cm:
            gamma.Gamma(fetch=fetch, retries=3)._get("/markets")
        self.assertEqual(cm.exception.status, 0)
        self.assertEqual(len(calls), 4)

    def test_http_transport_maps_timeout_to_status_0(self) -> None:
        import socket
        with mock.patch("gamma.urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            status, body = gamma.Gamma._http("https://example.invalid/x")
        self.assertEqual(status, 0)
        self.assertIn(b"timed out", body)

    def test_4xx_not_retried(self) -> None:
        calls = []
        def fetch(url: str) -> tuple[int, bytes]:
            calls.append(url)
            return 422, b"bad"
        with self.assertRaises(gamma.GammaError):
            gamma.Gamma(fetch=fetch)._get("/markets", offset=9999)
        self.assertEqual(len(calls), 1)

    def test_none_params_dropped(self) -> None:
        seen = []
        g = gamma.Gamma(fetch=lambda u: (seen.append(u), (200, b"[]"))[1])
        g._get("/markets", limit=1, slug=None)
        self.assertNotIn("slug", seen[0])


class RateLimitTests(unittest.TestCase):
    def _clocked(self, fetch, rate):
        t = {"now": 0.0, "slept": 0.0}
        def clock(): return t["now"]
        def sleep(s): t["slept"] += s; t["now"] += s
        return gamma.Gamma(fetch=fetch, rate=rate, clock=clock, sleep=sleep), t

    def test_bucket_paces_requests_to_the_rate(self) -> None:
        g, t = self._clocked(FakeGamma(_spread(10)), rate=10)
        for _ in range(30):
            g._get("/markets", limit=1)
        # 30 requests at 10/s with a 10-token burst: ~2 seconds of waiting
        self.assertAlmostEqual(t["slept"], 2.0, delta=0.15)

    def test_429_halves_the_rate_and_is_retried(self) -> None:
        calls = {"n": 0}
        def fetch(url):
            calls["n"] += 1
            return (429, b"") if calls["n"] == 1 else (200, b"[]")
        g, t = self._clocked(fetch, rate=8)
        self.assertEqual(g._get("/markets", limit=1), [])
        self.assertEqual(g.rate_limited, 1)
        self.assertEqual(g._rate, 4.0)
        self.assertGreaterEqual(t["slept"], 5.0)

    def test_429_recovers_after_a_run_of_successes(self) -> None:
        calls = {"n": 0}
        def fetch(url):
            calls["n"] += 1
            return (429, b"") if calls["n"] == 1 else (200, b"[]")
        g, _ = self._clocked(fetch, rate=8)
        for _ in range(201):
            g._get("/markets", limit=1)
        self.assertEqual(g._rate, 5.0)       # 4.0 * 1.25 after 200 OKs

    def test_persistent_429_raises(self) -> None:
        g, _ = self._clocked(lambda url: (429, b""), rate=8)
        with self.assertRaises(gamma.GammaError) as cm:
            g._get("/markets", limit=1)
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(g.rate_limited, 7)


class EnumerationTests(unittest.TestCase):
    def test_small_set_exhausts_plainly(self) -> None:
        fake = FakeGamma(_spread(250))
        g = gamma.Gamma(fetch=fake)
        self.assertEqual(len(list(g.markets_plain())), 250)
        self.assertEqual(len(g.markets_all()), 250)

    def test_plain_raises_at_wall(self) -> None:
        fake = FakeGamma(_spread(5000))
        g = gamma.Gamma(fetch=fake)
        seen = 0
        with self.assertRaises(gamma.TruncatedEnumeration):
            for _ in g.markets_plain():
                seen += 1
        self.assertEqual(seen, gamma.MAX_OFFSET + gamma.MAX_LIMIT)

    def test_partitioned_finds_everything_past_wall(self) -> None:
        fake = FakeGamma(_spread(5000))
        g = gamma.Gamma(fetch=fake)
        out = g.markets_all()
        self.assertEqual(len(out), 5000)
        self.assertEqual(set(out), {m["conditionId"] for m in fake.markets})

    def test_closed_filter_respected(self) -> None:
        ms = _spread(300)
        for m in ms[:50]:
            m["closed"] = True
        g = gamma.Gamma(fetch=FakeGamma(ms))
        self.assertEqual(len(g.markets_all()), 250)
        self.assertEqual(len(g.markets_all(closed=True)), 300)

    def test_markets_without_end_date_in_first_page_are_kept(self) -> None:
        ms = _spread(150)
        ms[0]["endDate"] = None
        g = gamma.Gamma(fetch=FakeGamma(ms))
        self.assertIn(ms[0]["conditionId"], g.markets_all())

    def test_raises_when_one_instant_holds_more_than_the_wall(self) -> None:
        ms = [_mk(i, "2027-01-01T00:00:00Z", start="2026-01-01T00:00:00Z") for i in range(2500)]
        g = gamma.Gamma(fetch=FakeGamma(ms))
        with self.assertRaises(gamma.TruncatedEnumeration):
            g.markets_all()

    def test_falls_back_to_start_date_axis(self) -> None:
        ms = [_mk(i, "2027-01-01T00:00:00Z", start=f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}T00:00:00Z")
              for i in range(2500)]
        g = gamma.Gamma(fetch=FakeGamma(ms))
        self.assertEqual(len(g.markets_all()), 2500)


class RulesTests(unittest.TestCase):
    def test_hash_is_byte_exact(self) -> None:
        self.assertNotEqual(rules_hash("a"), rules_hash("a "))
        self.assertEqual(rules_hash(None), rules_hash(""))
        self.assertEqual(len(rules_hash("x")), 64)

    def test_fingerprint_fields(self) -> None:
        fp = fingerprint(_mk(7, "2027-01-01T00:00:00Z"))
        self.assertEqual(set(fp), {"conditionId", "slug", "question", "sha256", "createdAt", "endDate"})


class StatsTests(unittest.TestCase):
    def test_compute_and_render(self) -> None:
        import stats
        ms = {m["conditionId"]: m for m in _spread(40)}
        ms[next(iter(ms))]["description"] = ""
        ms[next(iter(ms))]["negRisk"] = True
        ms[next(iter(ms))]["volumeNum"] = "2500"
        s = stats.compute(ms, "2026-06-01T00:00:00Z")
        self.assertEqual(s["open_markets"], 40)
        self.assertEqual(s["plain_pagination_sees"], 40)
        self.assertEqual(s["empty_description"], 1)
        self.assertEqual(s["neg_risk"], 1)
        self.assertEqual(s["volume"]["buckets"]["1k-10k"], 1)
        self.assertEqual(s["volume"]["zero"], 39)
        self.assertEqual(s["outcomes_count"], {"2": 40})
        self.assertEqual(sum(s["by_end_month"].values()), 40)
        md = stats.render(s, {"a": {"revisions": 2}, "b": {"revisions": 1}})
        self.assertIn("| Open markets | **40** |", md)
        self.assertIn("rules changed since the archive began | 1 |", md)

    def test_empty_set(self) -> None:
        import stats
        s = stats.compute({}, "2026-06-01T00:00:00Z")
        self.assertEqual(s["open_markets"], 0)
        self.assertIn("n/a", stats.render(s))


class WatcherTests(unittest.TestCase):
    def test_short_lived_markets_counted_but_not_archived(self) -> None:
        import tempfile, pathlib, watch
        ms = _spread(30)
        for m in ms[:10]:
            m["endDate"] = "2025-06-01T06:00:00Z"      # six hours after createdAt
        tmp = pathlib.Path(tempfile.mkdtemp())
        watch.ARCHIVE, watch.INDEX_DIR = tmp, tmp / "index"
        watch.RULES, watch.CHANGES, watch.RUNS = tmp / "rules", tmp / "changes", tmp / "runs.log"
        watch.STATS, watch.STATS_MD, watch.FEED = tmp / "stats.json", tmp / "STATS.md", tmp / "feed.xml"
        real = gamma.Gamma
        watch.gamma.Gamma = lambda: real(fetch=FakeGamma(ms))
        try:
            self.assertEqual(watch.run(), 0)
            self.assertEqual(len(watch._load_index()), 20)
            st = json.loads(watch.STATS.read_text())
            self.assertEqual(st["open_markets"], 30)
            self.assertEqual(st["archived"], 20)
            self.assertEqual(st["lifetime"]["<1 day"], 10)
            self.assertIn("markets=30 archived=20", watch.RUNS.read_text())
        finally:
            watch.gamma.Gamma = real

    def test_runs_detect_change_and_dry_run_writes_nothing(self) -> None:
        import tempfile, pathlib, watch
        ms = _spread(120)
        tmp = pathlib.Path(tempfile.mkdtemp())
        watch.ARCHIVE, watch.INDEX_DIR = tmp, tmp / "index"
        watch.RULES, watch.CHANGES, watch.RUNS = tmp / "rules", tmp / "changes", tmp / "runs.log"
        watch.STATS, watch.STATS_MD, watch.FEED = tmp / "stats.json", tmp / "STATS.md", tmp / "feed.xml"
        real = gamma.Gamma
        watch.gamma.Gamma = lambda: real(fetch=FakeGamma(ms))
        try:
            self.assertEqual(watch.run(), 0)
            idx = watch._load_index()
            self.assertEqual(len(idx), 120)
            self.assertEqual(sum(1 for _ in watch.RULES.rglob("*.txt")), 120)
            self.assertGreater(len(list(watch.INDEX_DIR.glob("*.json"))), 1)
            self.assertEqual(len(watch.RUNS.read_text().splitlines()), 1)
            self.assertFalse(watch.CHANGES.exists())
            self.assertNotIn("<entry>", watch.FEED.read_text())
            self.assertIn(f"<updated>{watch.RUNS.read_text().split()[0]}</updated>", watch.FEED.read_text())
            self.assertEqual(json.loads(watch.STATS.read_text())["open_markets"], 120)
            md = watch.STATS_MD.read_text()
            self.assertIn("| Open markets | **120** |", md)
            # the archived rows belong to the same table: no blank line before them
            self.assertIn("as reported) | 0 |\n| Archived (lifetime", md)
            ms[5]["description"] = "rules 5, edited"
            self.assertEqual(watch.run(), 0)
            idx = watch._load_index()
            self.assertEqual(idx[ms[5]["conditionId"]]["revisions"], 2)
            md = next(watch.CHANGES.glob("*.md")).read_text()
            self.assertIn("rules 5\n", md)
            self.assertIn("rules 5, edited", md)
            self.assertIn("```diff\n", md)
            self.assertIn("\n-rules 5\n+rules 5, edited\n", md)
            recs = json.loads(next(watch.CHANGES.glob("*.json")).read_text())
            self.assertEqual([r["conditionId"] for r in recs], [ms[5]["conditionId"]])
            self.assertEqual(recs[0]["revision"], 2)
            self.assertEqual(recs[0]["before"], "rules 5")
            self.assertEqual(recs[0]["after"], "rules 5, edited")
            self.assertEqual(recs[0]["sha256_after"], rules_hash("rules 5, edited"))
            xml_ = watch.FEED.read_text()
            self.assertIn(f"/rules/{ms[5]['conditionId']}/2</id>", xml_)
            self.assertIn("+rules 5, edited", xml_)
            self.assertEqual(len(watch.RUNS.read_text().splitlines()), 2)
            before = json.dumps(watch._load_index(), sort_keys=True)
            feed_before = watch.FEED.read_text()
            self.assertEqual(watch.run(dry_run=True), 0)
            self.assertEqual(json.dumps(watch._load_index(), sort_keys=True), before)
            self.assertEqual(watch.FEED.read_text(), feed_before)
        finally:
            watch.gamma.Gamma = real


class FeedTests(unittest.TestCase):
    def _dir(self):
        import tempfile, pathlib
        return pathlib.Path(tempfile.mkdtemp())

    def _changed(self, n: int, rev: int = 2, before: str = "Resolves YES if A.", after: str = "Resolves YES if B."):
        return [(f"0x{i:062x}", {"slug": f"m-{i}", "question": f"Q{i}?", "revisions": rev,
                                 "sha256": rules_hash(after)}, before, after) for i in range(n)]

    def test_record_then_build_is_valid_atom_newest_first(self) -> None:
        import feed
        from xml.etree import ElementTree as ET
        d = self._dir()
        feed.record(d / "changes", "2026-11-05T06:17:00Z", self._changed(2))
        feed.record(d / "changes", "2026-11-06T06:17:00Z", self._changed(1, rev=3, before="Resolves YES if B.", after="Resolves YES if C."))
        n = feed.build(d / "changes", d / "feed.xml", "2026-11-07T06:17:00Z")
        self.assertEqual(n, 3)
        root = ET.parse(d / "feed.xml").getroot()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        self.assertEqual(root.find("a:updated", ns).text, "2026-11-07T06:17:00Z")
        entries = root.findall("a:entry", ns)
        self.assertEqual([e.find("a:updated", ns).text for e in entries],
                         ["2026-11-06T06:17:00Z", "2026-11-05T06:17:00Z", "2026-11-05T06:17:00Z"])
        self.assertEqual(entries[0].find("a:id", ns).text,
                         "tag:throughline.markets,2026:polymarket/rules/0x" + "0" * 62 + "/3")
        self.assertEqual(entries[0].find("a:link[@rel='alternate']", ns).get("href"),
                         "https://polymarket.com/market/m-0")
        self.assertIn("/archive/rules/00/0x", entries[0].find("a:link[@rel='related']", ns).get("href"))
        content = entries[0].find("a:content", ns).text
        self.assertIn("-Resolves YES if B.\n+Resolves YES if C.", content)
        self.assertIn("sha256 before: " + rules_hash("Resolves YES if B."), content)
        # two feed-level ids must differ per revision: the same market at rev 2 is also present
        ids = {e.find("a:id", ns).text for e in entries}
        self.assertEqual(len(ids), 3)

    def test_markup_in_rules_text_is_escaped(self) -> None:
        import feed
        from xml.etree import ElementTree as ET
        d = self._dir()
        feed.record(d / "changes", "2026-11-05T06:17:00Z",
                    self._changed(1, before="a < b & c", after='<script>alert("x")</script>'))
        feed.build(d / "changes", d / "feed.xml")
        root = ET.parse(d / "feed.xml").getroot()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        content = root.find("a:entry/a:content", ns).text
        self.assertIn('+<script>alert("x")</script>', content)
        self.assertIn("-a < b & c", content)
        self.assertNotIn("<script>", (d / "feed.xml").read_text())

    def test_capped_at_max_entries_and_empty_feed_is_valid(self) -> None:
        import feed
        from xml.etree import ElementTree as ET
        d = self._dir()
        for day in range(1, 6):
            feed.record(d / "changes", f"2026-11-{day:02d}T06:17:00Z", self._changed(50))
        self.assertEqual(feed.build(d / "changes", d / "feed.xml"), feed.MAX_ENTRIES)
        self.assertEqual(len(feed.load(d / "changes")), 200)
        self.assertEqual(feed.load(d / "changes")[0]["seen"], "2026-11-05T06:17:00Z")
        self.assertEqual(feed.build(d / "empty", d / "none.xml"), 0)
        root = ET.parse(d / "none.xml").getroot()
        self.assertEqual(root.findall("{http://www.w3.org/2005/Atom}entry"), [])
        self.assertEqual(feed.record(d / "changes", "x", []), 0)

    def test_same_day_runs_append_and_identical_text_reports_no_diff(self) -> None:
        import feed
        d = self._dir()
        feed.record(d / "changes", "2026-11-05T06:17:00Z", self._changed(1))
        feed.record(d / "changes", "2026-11-05T18:00:00Z", self._changed(1, rev=3))
        self.assertEqual(len(json.loads((d / "changes" / "2026-11-05.json").read_text())), 2)
        self.assertEqual(feed.load(d / "changes")[0]["seen"], "2026-11-05T18:00:00Z")
        self.assertIn("no textual difference", feed.unified_diff("same", "same"))
        self.assertTrue(feed.unified_diff("a", "b").endswith("\n"))

    def test_cli(self) -> None:
        import subprocess, feed, watch
        d = self._dir()
        feed.record(d / "changes", "2026-11-05T06:17:00Z", self._changed(1))
        (d / "runs.log").write_text("2026-11-06T06:17:00Z ok markets=1\n")
        root = pathlib.Path(__file__).resolve().parent
        env = {"PYTHONPATH": str(root), "PATH": os.environ.get("PATH", "")}
        shim = d / "run.py"
        shim.write_text("import sys, pathlib, watch, feed\n"
                        f"watch.ARCHIVE = pathlib.Path({str(d)!r}); watch.CHANGES = watch.ARCHIVE / 'changes'\n"
                        "watch.RUNS = watch.ARCHIVE / 'runs.log'\n"
                        "sys.exit(feed.main(sys.argv[1:]))\n")
        r = subprocess.run([sys.executable, str(shim)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 entries", r.stdout)
        self.assertIn("<updated>2026-11-06T06:17:00Z</updated>", (d / "feed.xml").read_text())
        r = subprocess.run([sys.executable, str(shim), "--show", "5"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("== Q0?  (revision 2, seen 2026-11-05T06:17:00Z)", r.stdout)
        self.assertIn("+Resolves YES if B.", r.stdout)
        r = subprocess.run([sys.executable, str(shim), "--show"], capture_output=True, text=True,
                           env=env, cwd=str(self._dir()))
        self.assertIn("+Resolves YES if B.", r.stdout)


class SiteTests(unittest.TestCase):
    def test_builds_feed_index_and_nojekyll_from_archive(self) -> None:
        import tempfile, importlib.util, feed
        spec = importlib.util.spec_from_file_location("tl_site", pathlib.Path(__file__).with_name("site.py"))
        tl_site = importlib.util.module_from_spec(spec); spec.loader.exec_module(tl_site)
        d = pathlib.Path(tempfile.mkdtemp())
        feed.record(d / "changes", "2026-11-05T06:17:00Z",
                    [("0x" + "a" * 62, {"slug": "s", "question": "Q & A?", "revisions": 2,
                                        "sha256": rules_hash("b")}, "a", "b")])
        feed.build(d / "changes", d / "feed.xml")
        (d / "runs.log").write_text("x\n2026-11-05T06:17:00Z ok markets=1 <late>\n")
        out = tl_site.build(d / "site", d / "feed.xml", d / "runs.log")
        self.assertEqual((out / "feed.xml").read_text(), (d / "feed.xml").read_text())
        html = (out / "index.html").read_text()
        self.assertIn("Entries in the feed now: <strong>1</strong>", html)
        self.assertIn("markets=1 &lt;late&gt;", html)
        self.assertIn('href="feed.xml"', html)
        self.assertIn(feed.FEED_URL, html)
        self.assertTrue((out / ".nojekyll").exists())

    def test_builds_an_empty_site_when_nothing_has_run(self) -> None:
        import tempfile, importlib.util
        from xml.etree import ElementTree as ET
        spec = importlib.util.spec_from_file_location("tl_site", pathlib.Path(__file__).with_name("site.py"))
        tl_site = importlib.util.module_from_spec(spec); spec.loader.exec_module(tl_site)
        d = pathlib.Path(tempfile.mkdtemp())
        out = tl_site.build(d / "site", d / "missing.xml", d / "missing.log")
        self.assertEqual(ET.parse(out / "feed.xml").getroot().findall("{http://www.w3.org/2005/Atom}entry"), [])
        html = (out / "index.html").read_text()
        self.assertIn("<strong>0</strong>", html)
        self.assertIn("Last watcher run: <code>never</code>", html)


class ArchiveFixture(unittest.TestCase):
    """A temp git repo holding an archive built by watch.run() over a fake Gamma."""

    def setUp(self) -> None:
        import tempfile, pathlib, subprocess, watch
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=self.tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.tmp, check=True)
        a = self.tmp / "archive"
        watch.ARCHIVE, watch.INDEX_DIR = a, a / "index"
        watch.RULES, watch.CHANGES, watch.RUNS = a / "rules", a / "changes", a / "runs.log"
        watch.FEED = a / "feed.xml"
        watch.STATS, watch.STATS_MD = a / "stats.json", self.tmp / "STATS.md"
        self.ms = _spread(12)
        self.real = gamma.Gamma
        watch.gamma.Gamma = lambda: self.real(fetch=FakeGamma(self.ms))

    def tearDown(self) -> None:
        import watch
        watch.gamma.Gamma = self.real

    def _commit(self, msg: str) -> None:
        import subprocess
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=self.tmp, check=True)


class HistoryTests(ArchiveFixture):
    """history.py replays revisions from git."""

    def test_find_by_cid_slug_and_url(self) -> None:
        import history, watch
        self.assertEqual(watch.run(), 0)
        cid, slug = self.ms[3]["conditionId"], self.ms[3]["slug"]
        self.assertEqual(history.find(cid)[0], cid)
        self.assertEqual(history.find(slug)[0], cid)
        self.assertEqual(history.find(f"https://polymarket.com/market/{slug}?tid=1")[0], cid)
        self.assertIsNone(history.find("0xnotamarket"))
        self.assertIsNone(history.find("https://polymarket.com/event/nope"))

    def test_revisions_follow_git_history_and_verify_finds_old_hash(self) -> None:
        import history, watch
        from rules import rules_hash
        cid = self.ms[5]["conditionId"]
        self.assertEqual(watch.run(), 0)
        self._commit("run 1")
        first = rules_hash(self.ms[5]["description"])
        self.ms[5]["description"] = "rules 5, edited"
        self.assertEqual(watch.run(), 0)
        self._commit("run 2")
        revs = history.revisions(cid, root=self.tmp)
        self.assertEqual([r["sha256"] for r in revs], [first, rules_hash("rules 5, edited")])
        self.assertEqual(revs[0]["text"], "rules 5")
        self.assertTrue(all(r["date"] for r in revs))
        self.assertEqual(len(history.verify(cid, first.upper(), root=self.tmp)), 1)
        self.assertEqual(history.verify(cid, "0" * 64, root=self.tmp), [])
        # an unchanged file does not produce a phantom revision
        self.assertEqual(watch.run(), 0)
        self._commit("run 3")
        self.assertEqual(len(history.revisions(cid, root=self.tmp)), 2)

    def test_uncommitted_working_tree_revision_is_listed(self) -> None:
        import history, watch
        cid = self.ms[0]["conditionId"]
        self.assertEqual(watch.run(), 0)
        self._commit("run 1")
        self.ms[0]["description"] = "live edit"
        self.assertEqual(watch.run(), 0)
        revs = history.revisions(cid, root=self.tmp)
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[-1]["commit"], "(working tree)")
        self.assertEqual(revs[-1]["text"], "live edit")

    def test_never_archived_market(self) -> None:
        import history
        self.assertEqual(history.revisions("0x" + "ab" * 32, root=self.tmp), [])

    def test_cli(self) -> None:
        import history, watch, io, contextlib
        self.assertEqual(watch.run(), 0)
        self._commit("run 1")
        cid = self.ms[2]["conditionId"]
        history.ROOT = self.tmp
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(history.main(["history.py", cid]), 0)
        self.assertIn("--- revision 1", out.getvalue())
        self.assertIn("rules 2", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(["history.py", "0xmissing"]), 1)
            self.assertEqual(history.main(["history.py", cid, "--verify", "0" * 64]), 1)
            self.assertEqual(history.main(["history.py", cid, "--verify"]), 2)
            self.assertEqual(history.main(["history.py"]), 2)
        from rules import rules_hash
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(["history.py", cid, "--verify", rules_hash("rules 2")]), 0)


class McpTests(ArchiveFixture):
    """mcp_server.py over a temp archive (same fixture as HistoryTests) and a fake Gamma."""

    def _server(self):
        import mcp_server
        return mcp_server.Server(gamma_factory=lambda: self.real(fetch=FakeGamma(self.ms)), root=self.tmp)

    def _call(self, srv, name, **args):
        r = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        return r

    def test_protocol_handshake_and_errors(self) -> None:
        import mcp_server
        srv = self._server()
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r["result"]["serverInfo"]["name"], "throughline")
        self.assertIn("tools", r["result"]["capabilities"])
        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSIONS[0])
        self.assertIsNone(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(srv.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"], {})
        names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["market", "event_markets", "rules_history", "verify_rules_hash",
                                 "search_archive", "recent_rule_changes", "archive_stats"])
        for t in mcp_server.TOOLS:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(hasattr(srv, f"tool_{t['name']}"))
        self.assertEqual(srv.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"], -32601)
        self.assertEqual(srv.handle({"id": 6, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(srv.handle([1, 2])["error"]["code"], -32600)
        self.assertEqual(self._call(srv, "tool_market", ref="x")["error"]["code"], -32602)
        self.assertEqual(self._call(srv, "market")["error"]["code"], -32602)
        self.assertEqual(self._call(srv, "market", ref="a", extra=1)["error"]["code"], -32602)
        self.assertEqual(self._call(srv, "verify_rules_hash", ref="a", sha256="zz")["error"]["code"], -32602)
        self.assertEqual(self._call(srv, "search_archive", query="  ")["error"]["code"], -32602)

    def test_market_live_lookup_by_slug_and_condition_id(self) -> None:
        import watch
        self.assertEqual(watch.run(), 0)
        self._commit("seed")
        srv = self._server()
        m = self.ms[4]
        for ref in (m["slug"], f"https://polymarket.com/market/{m['slug']}", m["conditionId"], m["conditionId"].upper()):
            r = self._call(srv, "market", ref=ref)
            self.assertFalse(r["result"]["isError"], ref)
            d = r["result"]["structuredContent"]
            self.assertTrue(d["found"], ref)
            self.assertEqual(d["conditionId"], m["conditionId"])
            self.assertEqual(d["outcomes"], ["Yes", "No"])
            self.assertEqual(d["rules"], "rules 4")
            self.assertEqual(d["rules_sha256"], rules_hash("rules 4"))
            self.assertTrue(d["archived"])
            self.assertFalse(d["rules_changed_since_archived"])
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["slug"], m["slug"])
        m["description"] = "rules 4, edited live"
        d = self._call(srv, "market", ref=m["slug"])["result"]["structuredContent"]
        self.assertTrue(d["rules_changed_since_archived"])
        d = self._call(srv, "market", ref="no-such-market")["result"]["structuredContent"]
        self.assertFalse(d["found"])

    def test_event_markets_and_market_on_an_event_ref(self) -> None:
        srv = self._server()
        fake = FakeGamma(self.ms)
        fake.events = [{"slug": "big-event", "title": "Big", "markets": self.ms[:3]}]
        srv._gamma = self.real(fetch=fake)
        d = self._call(srv, "event_markets", ref="https://polymarket.com/event/big-event")["result"]["structuredContent"]
        self.assertTrue(d["found"])
        self.assertEqual([x["slug"] for x in d["markets"]], ["m-0", "m-1", "m-2"])
        d = self._call(srv, "market", ref="big-event")["result"]["structuredContent"]
        self.assertFalse(d["found"])
        self.assertEqual(len(d["conditionIds"]), 3)
        self.assertFalse(self._call(srv, "event_markets", ref="nope")["result"]["structuredContent"]["found"])

    def test_history_verify_search_changes_and_stats_are_offline(self) -> None:
        import watch
        self.assertEqual(watch.run(), 0)
        self._commit("day 1")
        self.ms[2]["description"] = "rules 2, edited"
        self.assertEqual(watch.run(), 0)
        self._commit("day 2")
        srv = self._server()
        srv._gamma_factory = lambda: (_ for _ in ()).throw(AssertionError("network used"))
        cid = self.ms[2]["conditionId"]
        d = self._call(srv, "rules_history", ref=self.ms[2]["slug"])["result"]["structuredContent"]
        self.assertTrue(d["archived"])
        self.assertEqual([r["text"] for r in d["revisions"]], ["rules 2", "rules 2, edited"])
        self.assertEqual(d["index"]["revisions"], 2)
        self.assertFalse(self._call(srv, "rules_history", ref="0xnot")["result"]["structuredContent"]["archived"])
        d = self._call(srv, "verify_rules_hash", ref=cid, sha256=rules_hash("rules 2").upper())["result"]["structuredContent"]
        self.assertTrue(d["match"]); self.assertFalse(d["current"])
        self.assertEqual(d["matches"][0]["sha256"], rules_hash("rules 2"))
        self.assertNotIn("text", d["matches"][0])
        d = self._call(srv, "verify_rules_hash", ref=cid, sha256=rules_hash("rules 2, edited"))["result"]["structuredContent"]
        self.assertTrue(d["match"]); self.assertTrue(d["current"])
        d = self._call(srv, "verify_rules_hash", ref=cid, sha256="0" * 64)["result"]["structuredContent"]
        self.assertFalse(d["match"])
        d = self._call(srv, "verify_rules_hash", ref="0xnot", sha256="0" * 64)["result"]["structuredContent"]
        self.assertFalse(d["archived"])
        d = self._call(srv, "search_archive", query="q1")["result"]["structuredContent"]
        self.assertEqual(d["total"], 3)          # Q1, Q10, Q11
        self.assertEqual([x["slug"] for x in d["markets"]], ["m-1", "m-10", "m-11"])
        d = self._call(srv, "search_archive", query="M-1", limit=1)["result"]["structuredContent"]
        self.assertEqual((d["total"], d["returned"], len(d["markets"])), (3, 1, 1))
        self.assertEqual(self._call(srv, "search_archive", query="zzz")["result"]["structuredContent"]["total"], 0)
        d = self._call(srv, "recent_rule_changes")["result"]["structuredContent"]
        self.assertEqual(len(d["changes"]), 1)
        self.assertEqual(d["changes"][0]["conditionId"], cid)
        self.assertIn("-rules 2\n+rules 2, edited", d["changes"][0]["diff"])
        d = self._call(srv, "archive_stats")["result"]["structuredContent"]
        self.assertTrue(d["available"])
        self.assertEqual(d["stats"]["open_markets"], 12)
        self.assertEqual(d["runs"], 2)
        self.assertEqual(d["archived_markets"], 12)
        self.assertIn("changed=1", d["last_run"])

    def test_gamma_failure_is_a_tool_error_not_a_protocol_error(self) -> None:
        srv = self._server()
        srv._gamma = self.real(fetch=lambda url: (500, b"boom"), retries=1)
        r = self._call(srv, "market", ref="m-1")
        self.assertIn("result", r)
        self.assertTrue(r["result"]["isError"])
        self.assertIn("Gamma API error", r["result"]["content"][0]["text"])
        srv2 = self._server()
        srv2.tool_archive_stats = lambda: 1 / 0
        r = self._call(srv2, "archive_stats")
        self.assertTrue(r["result"]["isError"])
        self.assertIn("ZeroDivisionError", r["result"]["content"][0]["text"])

    def test_stdio_framing_in_a_subprocess(self) -> None:
        import subprocess
        root = pathlib.Path(__file__).resolve().parent
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "",
            "{not json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
        ]
        r = subprocess.run([sys.executable, str(root / "mcp_server.py")], input="\n".join(lines) + "\n",
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual([o.get("id") for o in out], [1, None, 2, 3])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(out[1]["error"]["code"], -32700)
        self.assertEqual(len(out[3]["result"]["tools"]), 7)
        r = subprocess.run([sys.executable, str(root / "mcp_server.py"), "--tools"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("verify_rules_hash:", r.stdout)


class DumpTests(ArchiveFixture):
    """dump.py writes one gzipped JSON Lines snapshot of the archive plus checksums."""

    def _rows(self, path):
        import gzip
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return [json.loads(l) for l in fh]

    def test_snapshot_has_every_market_and_every_change_with_valid_sums(self) -> None:
        import dump, hashlib, watch
        self.assertEqual(watch.run(), 0)
        self.ms[2]["description"] = "rules 2, edited"
        self.ms[5]["closed"] = True
        self.assertEqual(watch.run(), 0)
        out = self.tmp / "dist"
        m = dump.build(out)
        self.assertEqual((m["markets"], m["changes"]), (12, 1))
        self.assertEqual(m["date"], m["last_run"][:10])
        self.assertEqual(m["tag"], f"dump-{m['date']}")
        self.assertEqual(sorted(m["files"]), sorted([f"markets-{m['date']}.jsonl.gz", f"changes-{m['date']}.jsonl.gz",
                                                     f"stats-{m['date']}.json", "SHA256SUMS"]))
        rows = self._rows(out / f"markets-{m['date']}.jsonl.gz")
        self.assertEqual([r["conditionId"] for r in rows], sorted(x["conditionId"] for x in self.ms))
        by = {r["slug"]: r for r in rows}
        self.assertEqual(by["m-2"]["rules"], "rules 2, edited")
        self.assertEqual(by["m-2"]["revisions"], 2)
        self.assertEqual(by["m-2"]["sha256"], rules_hash("rules 2, edited"))
        self.assertEqual(by["m-1"]["rules"], "rules 1")
        self.assertNotIn("last_seen_open", by["m-1"])
        self.assertIn("last_seen_open", by["m-5"])
        self.assertEqual(by["m-5"]["url"], "https://polymarket.com/market/m-5")
        changes = self._rows(out / f"changes-{m['date']}.jsonl.gz")
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["before"], changes[0]["after"], changes[0]["revision"]),
                         ("rules 2", "rules 2, edited", 2))
        self.assertEqual(json.loads((out / f"stats-{m['date']}.json").read_text())["open_markets"], 11)  # m-5 closed
        for line in (out / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual(len((out / "SHA256SUMS").read_text().splitlines()), 3)
        n = dump.notes(m)
        self.assertIn("| Archived markets | 12 |", n)
        self.assertIn("| Recorded rules edits | 1 |", n)
        self.assertIn(m["last_run"], n)
        self.assertIn(f"`markets-{m['date']}.jsonl.gz`", n)

    def test_empty_archive_still_builds(self) -> None:
        import dump, watch
        out = self.tmp / "dist"
        m = dump.build(out)
        self.assertEqual((m["markets"], m["changes"], m["last_run"]), (0, 0, None))
        self.assertEqual(m["date"], watch._now().strftime("%Y-%m-%d"))
        self.assertNotIn(f"stats-{m['date']}.json", m["files"])
        self.assertEqual(self._rows(out / f"markets-{m['date']}.jsonl.gz"), [])
        self.assertEqual(len((out / "SHA256SUMS").read_text().splitlines()), 2)
        self.assertIn("| Last watcher run | `none` |", dump.notes(m))

    def test_cli_writes_manifest_and_prints_notes(self) -> None:
        import os, subprocess, watch
        self.assertEqual(watch.run(), 0)
        root = pathlib.Path(__file__).resolve().parent
        shim = self.tmp / "shim.py"
        shim.write_text(f"import sys, watch\n"
                        f"watch.ARCHIVE = __import__('pathlib').Path({str(self.tmp / 'archive')!r})\n"
                        f"watch.INDEX_DIR, watch.RULES, watch.CHANGES = watch.ARCHIVE/'index', watch.ARCHIVE/'rules', watch.ARCHIVE/'changes'\n"
                        f"watch.RUNS, watch.STATS = watch.ARCHIVE/'runs.log', watch.ARCHIVE/'stats.json'\n"
                        f"import dump; sys.exit(dump.main(sys.argv[1:]))\n")
        env = dict(os.environ, PYTHONPATH=str(root))
        out = self.tmp / "dist"
        r = subprocess.run([sys.executable, str(shim), str(out)], capture_output=True, text=True, env=env, cwd=str(root))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("12 markets, 0 changes, tag dump-", r.stdout)
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["markets"], 12)
        r = subprocess.run([sys.executable, str(shim), "--notes", str(out)], capture_output=True, text=True, env=env, cwd=str(root))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| Archived markets | 12 |", r.stdout)


# -- live ---------------------------------------------------------------------

@unittest.skipUnless(LIVE, "pass --live")
class LiveGotchaTests(unittest.TestCase):
    """Each test is one claim in GOTCHAS.md. A failure means the API changed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.g = gamma.Gamma()

    def test_1_limit_clamps_to_100_with_http_200(self) -> None:
        self.assertEqual(len(self.g._get("/markets", limit=500)), 100)

    def test_2_offset_wall_returns_422(self) -> None:
        with self.assertRaises(gamma.GammaError) as cm:
            self.g._get("/markets", limit=100, offset=2500, closed="false")
        self.assertEqual(cm.exception.status, 422)

    def test_2b_filters_do_not_move_the_wall(self) -> None:
        with self.assertRaises(gamma.GammaError) as cm:
            self.g._get("/markets", limit=100, offset=2500, closed="false", end_date_min="2020-01-01T00:00:00Z")
        self.assertEqual(cm.exception.status, 422)

    def test_3_date_filters_genuinely_filter_and_are_inclusive(self) -> None:
        m = self.g._get("/markets", limit=1, closed="false")[0]
        end = m["endDate"]
        t = gamma._epoch(end)
        with self.assertRaises(gamma.GammaError) as cm:     # equal bounds are rejected
            self.g._get("/markets", limit=1, closed="false", end_date_min=end, end_date_max=end)
        self.assertEqual(cm.exception.status, 422)
        # min inclusive: [end, end+1s] contains the market; max inclusive: [end-1s, end] does too
        for lo, hi in ((t, t + 1), (t - 1, t)):
            rows = self.g._get("/markets", limit=100, closed="false",
                               end_date_min=gamma._iso(lo), end_date_max=gamma._iso(hi))
            self.assertTrue(all(gamma._epoch(r["endDate"]) in (lo, hi) for r in rows))
            self.assertIn(m["conditionId"], {r["conditionId"] for r in rows})

    def test_4_unknown_params_are_ignored(self) -> None:
        base = [r["id"] for r in self.g._get("/markets", limit=5, closed="false")]
        bogus = [r["id"] for r in self.g._get("/markets", limit=5, closed="false", id_min=999999999)]
        self.assertEqual(base, bogus)

    def test_5_four_fields_are_json_strings(self) -> None:
        m = self.g._get("/markets", limit=1, closed="false")[0]
        for k in gamma._STRING_JSON:
            self.assertIsInstance(m.get(k), str, k)
            json.loads(m[k])

    def test_6_updated_at_is_shared_across_markets(self) -> None:
        rows = self.g._get("/markets", limit=100, closed="false", offset=1000)
        stamps = [r.get("updatedAt") for r in rows]
        self.assertLess(len(set(stamps)), len(stamps) // 2, "updatedAt looks per-market now")

    def test_7_description_present_no_revision_field(self) -> None:
        m = self.g._get("/markets", limit=1, closed="false")[0]
        self.assertIsInstance(m.get("description"), str)
        self.assertFalse({k for k in m if "revision" in k.lower() or "history" in k.lower()})

    def test_8_plain_walk_hits_the_wall(self) -> None:
        plain = 0
        try:
            for _ in gamma.Gamma().markets_plain():
                plain += 1
        except gamma.TruncatedEnumeration:
            pass
        self.assertEqual(plain, gamma.MAX_OFFSET + gamma.MAX_LIMIT)

    def test_9_one_day_slice_exceeds_the_wall(self) -> None:
        # GOTCHAS item 3: the day after the measurement date alone held more
        # than 2,100 open markets. Any day with a full page at the wall proves
        # that plain pagination cannot be complete. Scan the next 14 days.
        g = gamma.Gamma()
        today = datetime.now(timezone.utc).date()
        for d in range(1, 15):
            day = today + timedelta(days=d)
            rows = g._get("/markets", limit=gamma.MAX_LIMIT, offset=gamma.MAX_OFFSET, closed="false",
                          end_date_min=f"{day}T00:00:00Z", end_date_max=f"{day}T23:59:59Z")
            if len(rows) == gamma.MAX_LIMIT:
                return
        self.fail("no single day in the next 14 held more than 2,100 open markets")

    def test_10_closed_false_includes_past_end_dates(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = gamma.Gamma()._get("/markets", limit=5, closed="false", end_date_max=now)
        self.assertTrue(rows)
        self.assertTrue(all(m["closed"] is False for m in rows))

    def test_11_active_is_not_a_filter(self) -> None:
        g = gamma.Gamma()
        base = [m["id"] for m in g._get("/markets", limit=5, closed="false")]
        for extra in ({"active": "true"}, {"active": "false"}, {"archived": "true"}):
            self.assertEqual([m["id"] for m in g._get("/markets", limit=5, closed="false", **extra)], base)


if __name__ == "__main__":
    unittest.main(verbosity=1)
