"""
Tests. Offline tests use a fake transport and run in under a second:

    python3 test.py

Live tests hit the real Gamma API and re-measure every claim in GOTCHAS.md.
If one fails, the API changed and the doc is stale:

    python3 test.py --live
"""
from __future__ import annotations

import json
import sys
import unittest
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
        if path == "/events":
            return 200, b"[]"
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
            if lo:
                rows = [m for m in rows if m.get(field) and m[field] >= lo]
            if hi:
                rows = [m for m in rows if m.get(field) and m[field] <= hi]
        return 200, json.dumps(rows[offset:offset + limit]).encode()


def _mk(i: int, end: str, start: str = "2026-01-01T00:00:00Z", closed: bool = False) -> dict:
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
            m["endDate"] = "2026-01-01T06:00:00Z"      # six hours after createdAt
        tmp = pathlib.Path(tempfile.mkdtemp())
        watch.ARCHIVE, watch.INDEX_DIR = tmp, tmp / "index"
        watch.RULES, watch.CHANGES, watch.RUNS = tmp / "rules", tmp / "changes", tmp / "runs.log"
        watch.STATS, watch.STATS_MD = tmp / "stats.json", tmp / "STATS.md"
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
        watch.STATS, watch.STATS_MD = tmp / "stats.json", tmp / "STATS.md"
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
            self.assertEqual(json.loads(watch.STATS.read_text())["open_markets"], 120)
            self.assertIn("| Open markets | **120** |", watch.STATS_MD.read_text())
            ms[5]["description"] = "rules 5, edited"
            self.assertEqual(watch.run(), 0)
            idx = watch._load_index()
            self.assertEqual(idx[ms[5]["conditionId"]]["revisions"], 2)
            md = next(watch.CHANGES.iterdir()).read_text()
            self.assertIn("rules 5\n", md)
            self.assertIn("rules 5, edited", md)
            self.assertEqual(len(watch.RUNS.read_text().splitlines()), 2)
            before = json.dumps(watch._load_index(), sort_keys=True)
            self.assertEqual(watch.run(dry_run=True), 0)
            self.assertEqual(json.dumps(watch._load_index(), sort_keys=True), before)
        finally:
            watch.gamma.Gamma = real


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
        rows = self.g._get("/markets", limit=100, closed="false", end_date_min=end, end_date_max=end)
        self.assertTrue(all(r["endDate"] == end for r in rows))
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

    def test_8_partitioned_beats_plain(self) -> None:
        plain = 0
        try:
            for _ in gamma.Gamma().markets_plain():
                plain += 1
        except gamma.TruncatedEnumeration:
            pass
        self.assertEqual(plain, gamma.MAX_OFFSET + gamma.MAX_LIMIT)
        everything = gamma.Gamma().markets_all()
        self.assertGreater(len(everything), plain * 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
