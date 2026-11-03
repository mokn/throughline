import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from throughline.client import (  # noqa: E402
    Change,
    MatchStore,
    Page,
    RateLimited,
    Throughline,
    ThroughlineError,
    _parse_page,
    _retry_after,
)

EXAMPLE = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "schema/upsert-single.json").read_text()
)


def change(cursor, operation="upsert", record=None, stream="s", rid="r"):
    return Change(cursor, stream, rid, operation, record, "ThroughlinePublicationV3")


class ParsePage(unittest.TestCase):
    def test_parses_the_published_example(self):
        page = _parse_page(EXAMPLE, fallback_cursor=0)
        self.assertEqual(len(page.changes), 1)
        self.assertEqual(page.next_cursor, 1)
        self.assertFalse(page.has_more)
        c = page.changes[0]
        self.assertEqual(c.schema, "ThroughlinePublicationV3")
        self.assertEqual(c.operation, "upsert")
        self.assertEqual(c.key, ("test_stream", EXAMPLE["items"][0]["envelope"]["record_id"]))
        self.assertFalse(c.is_withdrawal)

    def test_empty_page_keeps_caller_moving(self):
        page = _parse_page({"items": [], "next_cursor": 97, "has_more": False}, fallback_cursor=12)
        self.assertEqual(page.changes, [])
        self.assertEqual(page.next_cursor, 97)

    def test_missing_next_cursor_does_not_reset_to_zero(self):
        page = _parse_page({"items": []}, fallback_cursor=4096)
        self.assertEqual(page.next_cursor, 4096)

    def test_missing_items_key(self):
        self.assertEqual(_parse_page({"next_cursor": 1}, fallback_cursor=0).changes, [])

    def test_null_record_is_a_withdrawal(self):
        page = _parse_page(
            {"items": [{"cursor": 9, "envelope": {"stream_id": "s", "record_id": "r",
             "operation": "withdraw", "record": None}}], "next_cursor": 9, "has_more": False},
            fallback_cursor=0,
        )
        self.assertTrue(page.changes[0].is_withdrawal)

    def test_unknown_operation_without_record_still_removes(self):
        self.assertTrue(change(1, operation="tombstone", record=None).is_withdrawal)

    def test_unknown_operation_with_record_is_not_a_withdrawal(self):
        self.assertFalse(change(1, operation="replace", record={"a": 1}).is_withdrawal)


class Store(unittest.TestCase):
    def setUp(self):
        self.store = MatchStore()
        self.rec = {"market": {"condition_id": "0xabc"}, "title": "first"}

    def test_upsert_then_withdrawal_then_reinstatement(self):
        self.store.apply(Page([change(1, record=self.rec)], 1, False))
        self.assertEqual(len(self.store), 1)
        self.store.apply(Page([change(2, "withdraw", None)], 2, False))
        self.assertEqual(len(self.store), 0)
        self.store.apply(Page([change(3, record=self.rec)], 3, False))
        self.assertEqual(len(self.store), 1)

    def test_upsert_replaces_rather_than_merges(self):
        self.store.apply(Page([change(1, record={"a": 1, "b": 2})], 1, False))
        self.store.apply(Page([change(2, record={"a": 9})], 2, False))
        self.assertEqual(self.store.current(), [{"a": 9}])

    def test_replay_is_idempotent(self):
        page = Page([change(1, record=self.rec)], 1, False)
        self.store.apply(page)
        self.store.apply(page)
        self.store.apply(page)
        self.assertEqual(len(self.store), 1)
        self.assertEqual(self.store.cursor, 1)

    def test_replaying_a_stale_page_cannot_resurrect_a_withdrawal(self):
        self.store.apply(Page([change(1, record=self.rec)], 1, False))
        self.store.apply(Page([change(2, "withdraw", None)], 2, False))
        self.store.apply(Page([change(1, record=self.rec)], 1, False))
        self.assertEqual(len(self.store), 0)

    def test_cursor_never_goes_backwards(self):
        self.store.apply(Page([], 50, False))
        self.store.apply(Page([], 10, False))
        self.assertEqual(self.store.cursor, 50)

    def test_two_streams_sharing_a_record_id_are_distinct_matches(self):
        self.store.apply(Page([change(1, record={"t": "a"}, stream="x")], 1, False))
        self.store.apply(Page([change(2, record={"t": "b"}, stream="y")], 2, False))
        self.assertEqual(len(self.store), 2)

    def test_for_market_filters_and_tolerates_missing_market(self):
        self.store.apply(Page([change(1, record=self.rec, rid="r1")], 1, False))
        self.store.apply(Page([change(2, record={"title": "no market"}, rid="r2")], 2, False))
        self.assertEqual(len(self.store.for_market("0xabc")), 1)
        self.assertEqual(self.store.for_market("0xdef"), [])


class RetryAfter(unittest.TestCase):
    def test_values(self):
        self.assertEqual(_retry_after("12"), 12.0)
        self.assertEqual(_retry_after("0"), 0.0)
        self.assertEqual(_retry_after(None), 5.0)
        self.assertEqual(_retry_after(""), 5.0)
        self.assertEqual(_retry_after("soon"), 5.0)
        self.assertEqual(_retry_after("-3"), 0.0)


def ok(body):
    return mock.MagicMock(
        __enter__=lambda s: mock.Mock(read=lambda: json.dumps(body).encode()),
        __exit__=lambda *a: False,
    )


def raise_http(code, body=b"{}", headers=None):
    err = urllib.error.HTTPError("http://x", code, "err", headers or {}, None)
    err.read = lambda: body
    raise err


class ClientTransport(unittest.TestCase):
    def test_requires_base_url_and_key(self):
        with self.assertRaises(ValueError):
            Throughline("", "k")
        with self.assertRaises(ValueError):
            Throughline("http://x", "")

    def test_sends_bearer_token_and_omits_none_params(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return ok(EXAMPLE)

        with mock.patch("urllib.request.urlopen", fake):
            Throughline("https://api.example/", "tl_secret").page(0)
        self.assertEqual(captured["auth"], "Bearer tl_secret")
        self.assertIn("cursor=0", captured["url"])
        self.assertNotIn("market_id", captured["url"])
        self.assertNotIn("limit", captured["url"])

    def test_429_honours_retry_after_then_succeeds(self):
        slept = []
        state = {"n": 0}

        def fake(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                raise_http(429, b"slow down", {"Retry-After": "7"})
            return ok(EXAMPLE)

        client = Throughline("https://api.example", "k", sleep=slept.append)
        with mock.patch("urllib.request.urlopen", fake):
            page = client.page(0)
        self.assertEqual(slept, [7.0])
        self.assertEqual(len(page.changes), 1)

    def test_429_gives_up_after_max_retries(self):
        def fake(req, timeout=None):
            raise_http(429, b"nope", {"Retry-After": "1"})

        client = Throughline("https://api.example", "k", max_retries=2, sleep=lambda _: None)
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(RateLimited) as ctx:
                client.page(0)
        self.assertEqual(ctx.exception.retry_after, 1.0)
        self.assertEqual(ctx.exception.status, 429)

    def test_client_errors_raise_without_retrying(self):
        for code in (400, 401, 404, 409, 413):
            calls = []

            def fake(req, timeout=None, _c=code):
                calls.append(1)
                raise_http(_c)

            client = Throughline("https://api.example", "k", sleep=lambda _: None)
            with mock.patch("urllib.request.urlopen", fake):
                with self.assertRaises(ThroughlineError) as ctx:
                    client.page(0)
            self.assertEqual(ctx.exception.status, code)
            self.assertEqual(len(calls), 1, f"{code} must not be retried")

    def test_match_returns_none_for_a_withdrawn_head(self):
        body = {"envelope": {"stream_id": "s", "record_id": "r",
                             "operation": "withdraw", "record": None}}
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: ok(body)):
            self.assertIsNone(Throughline("https://api.example", "k").match("r", stream_id="s"))

    def test_changes_walks_to_the_head_and_advances_on_empty_pages(self):
        pages = [
            {"items": [{"cursor": 1, "envelope": {"stream_id": "s", "record_id": "a",
             "operation": "upsert", "record": {}}}], "next_cursor": 1, "has_more": True},
            {"items": [], "next_cursor": 2, "has_more": True},
            {"items": [{"cursor": 3, "envelope": {"stream_id": "s", "record_id": "b",
             "operation": "upsert", "record": {}}}], "next_cursor": 3, "has_more": False},
        ]
        seen = []

        def fake(req, timeout=None):
            seen.append(req.full_url)
            return ok(pages.pop(0))

        client = Throughline("https://api.example", "k")
        with mock.patch("urllib.request.urlopen", fake):
            collected = list(client.changes(0))
        self.assertEqual(len(collected), 3)
        self.assertEqual(sum(len(p.changes) for p in collected), 2)
        self.assertIn("cursor=1", seen[1])
        self.assertIn("cursor=2", seen[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
