"""
Polymarket Gamma API client that does not return partial answers that look complete.

Gamma (https://gamma-api.polymarket.com) is the public, unauthenticated read API
for Polymarket markets. Measured against the live API on 2026-11-03 it does the
following, none of which is documented:

1. ``limit`` is silently clamped to 100. Ask for 500, get 100 rows and HTTP 200.
2. ``offset`` returns HTTP 422 above roughly 2000, and adding filters does not
   move the wall. Plain pagination therefore stops at ~2100 markets.
3. Those ~2100 are under 1% of the open markets. Partitioning the walk by
   ``end_date_min``/``end_date_max`` found 238,661 distinct open markets.
   ``active`` and ``archived`` are not filters; ``closed`` is.
4. Unknown query parameters are ignored, not rejected. ``id_min=…`` returns
   HTTP 200 and the unfiltered result set.
5. ``outcomes``, ``outcomePrices``, ``clobTokenIds`` and ``umaResolutionStatuses``
   are JSON-encoded *strings*, not arrays.
6. ``updatedAt`` is a bulk batch timestamp shared across unrelated markets.
   It cannot tell you that a market's rules changed.
7. ``description`` holds the resolution criteria with no revision history.

The guiding rule here is: never return a partial answer that looks complete.
Enumeration either covers every reachable market or raises.

Stdlib only. No credentials. See GOTCHAS.md for the probes behind each claim
(ten items; this list is the short version).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

BASE = "https://gamma-api.polymarket.com"
MAX_LIMIT = 100          # measured clamp; asking for more changes nothing
MAX_OFFSET = 2000        # last offset that returned 200; ~2031 returned 422
_STRING_JSON = ("outcomes", "outcomePrices", "clobTokenIds", "umaResolutionStatuses")
_MIN_SLICE_SECONDS = 60  # below this width on one axis, switch axis, then give up
_EPOCH_LO = 0            # 1970-01-01
_EPOCH_HI = 4102444800   # 2100-01-01

Fetch = Callable[[str], tuple[int, bytes]]


class GammaError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class TruncatedEnumeration(RuntimeError):
    """Raised instead of returning a short list.

    A caller that believes it enumerated every market when it saw a subset
    will publish a snapshot with holes in it and never know. Raising is the
    only honest behaviour when the API will not let us finish.
    """


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> int | None:
    if not iso:
        return None
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def _b(v: bool) -> str:
    return "true" if v else "false"


def slug_of(url_or_slug: str) -> str:
    """Accept a full URL, a path, or a bare slug and return the slug.

    Strips ``/event/``, ``/market/`` and ``/markets/`` path segments and any
    query string. Does not validate that the slug exists.
    """
    s = url_or_slug.strip()
    if "://" in s:
        s = urllib.parse.urlsplit(s).path
    s = s.split("?", 1)[0].strip("/")
    parts = [p for p in s.split("/") if p]
    while parts and parts[0] in ("event", "market", "markets"):
        parts.pop(0)
    return parts[-1] if parts else ""


def normalize(market: dict[str, Any]) -> dict[str, Any]:
    """Decode the string-JSON fields in place-safe copy.

    Values that are not valid JSON are left exactly as received, so a format
    change upstream shows up as a string instead of becoming an empty list.
    """
    out = dict(market)
    for k in _STRING_JSON:
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except ValueError:
                pass
    return out


class Gamma:
    """Client. ``rate`` is a client-side ceiling in requests per second, shared
    across threads; the API answers 429 to sustained bursts well above it and
    the client then halves its own rate, recovering slowly after a run of
    successes. ``clock``/``sleep`` are injectable for tests."""

    def __init__(self, fetch: Fetch | None = None, base: str = BASE, retries: int = 6,
                 progress: Callable[[int], None] | None = None, rate: float = 8.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._fetch = fetch or self._http
        self._base = base.rstrip("/")
        self._retries = retries
        self._progress = progress
        self._clock, self._sleep = clock, sleep
        self._rate0 = self._rate = float(rate)
        self._tokens = float(rate)
        self._last = clock()
        self._lock = threading.Lock()
        self._ok_streak = 0
        self.requests = 0
        self.rate_limited = 0

    def _acquire(self) -> None:
        """Token bucket; callers queue in order when the bucket is empty."""
        with self._lock:
            now = self._clock()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            self._tokens -= 1
            wait = max(0.0, -self._tokens / self._rate)
        if wait > 0:
            self._sleep(wait)

    def _throttle(self) -> None:
        with self._lock:
            self.rate_limited += 1
            self._ok_streak = 0
            self._rate = max(0.5, self._rate / 2)
            self._tokens = min(self._tokens, 0.0)

    def _recover(self) -> None:
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak % 200 == 0 and self._rate < self._rate0:
                self._rate = min(self._rate0, self._rate * 1.25)

    # -- transport -----------------------------------------------------------

    @staticmethod
    def _http(url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-gamma-tools/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, str(e).encode()      # status 0 = no response; retried like a 5xx

    def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{self._base}{path}?{urllib.parse.urlencode(clean)}" if clean else f"{self._base}{path}"
        for attempt in range(self._retries + 1):
            self._acquire()
            with self._lock:
                self.requests += 1
                n = self.requests
            if self._progress and n % 50 == 0:
                self._progress(n)
            status, body = self._fetch(url)
            if status == 200:
                self._recover()
                return json.loads(body)
            if status == 429:
                self._throttle()
                if attempt < self._retries:
                    self._sleep(max(5.0, 2.0 ** attempt))
                    continue
            elif status == 0 or status >= 500:
                if attempt < self._retries:
                    self._sleep(2.0 ** attempt)
                    continue
            raise GammaError(status, body[:200].decode("utf-8", "replace"))
        raise GammaError(0, "unreachable")

    # -- single lookups ------------------------------------------------------

    def market_by_slug(self, url_or_slug: str) -> dict[str, Any] | None:
        slug = slug_of(url_or_slug)
        rows = self._get("/markets", slug=slug, limit=1)
        return rows[0] if rows else None

    def condition_ids(self, url_or_slug: str) -> list[str]:
        """Resolve a market or event URL to condition IDs.

        An event URL fans out to every market under it (128 for a presidential
        nominee event), which is why this returns a list.
        """
        m = self.market_by_slug(url_or_slug)
        if m:
            return [m["conditionId"]]
        ev = self._get("/events", slug=slug_of(url_or_slug), limit=1)
        if not ev:
            return []
        return [m["conditionId"] for m in ev[0].get("markets", []) if m.get("conditionId")]

    # -- enumeration ---------------------------------------------------------

    def _walk(self, **filters: Any) -> tuple[list[dict[str, Any]], bool]:
        """Walk one filtered query to exhaustion or to the wall.

        Returns (rows, complete). ``complete`` is False when the wall was hit
        before the result set ran out.
        """
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._get("/markets", limit=MAX_LIMIT, offset=offset, **filters)
            rows.extend(page)
            if len(page) < MAX_LIMIT:
                return rows, True
            offset += MAX_LIMIT
            if offset > MAX_OFFSET:
                return rows, False

    def markets_all(self, *, closed: bool = False, workers: int = 4) -> dict[str, dict[str, Any]]:
        """Every reachable market, keyed by condition ID, or raise.

        Bisects the end-date axis. Each slice is probed at offset 0 and at the
        offset wall before anything is walked, so an oversized slice costs two
        requests, not twenty-one. Slices are processed concurrently. Markets
        without an end date cannot be reached by date filters; the first 2,000
        rows of a plain walk are unioned in so those are included when they
        sort early. Raises TruncatedEnumeration rather than returning a subset.
        """
        out: dict[str, dict[str, Any]] = {}
        lock = threading.Lock()
        base = {"closed": _b(closed)}

        def add(rows: list[dict[str, Any]]) -> None:
            with lock:
                for m in rows:
                    key = m.get("conditionId") or m.get("id")
                    if key:
                        out[key] = m

        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending: set = set()

            def submit(lo: int, hi: int, axis: str, filters: dict[str, Any]) -> None:
                pending.add(pool.submit(node, lo, hi, axis, filters))

            def node(lo: int, hi: int, axis: str, filters: dict[str, Any]) -> None:
                f = {**filters, f"{axis}_min": _iso(lo), f"{axis}_max": _iso(hi)}
                first = self._get("/markets", limit=MAX_LIMIT, offset=0, **f)
                add(first)
                if len(first) < MAX_LIMIT:
                    return
                last = self._get("/markets", limit=MAX_LIMIT, offset=MAX_OFFSET, **f)
                if len(last) == MAX_LIMIT:
                    # more than the wall allows: split instead of walking
                    if hi - lo > _MIN_SLICE_SECONDS:
                        mid = (lo + hi) // 2
                        with lock:
                            submit(lo, mid, axis, filters)
                            submit(mid + 1, hi, axis, filters)   # both bounds inclusive
                    elif axis == "end_date":
                        with lock:
                            submit(_EPOCH_LO, _EPOCH_HI, "start_date",
                                   {**filters, "end_date_min": _iso(lo), "end_date_max": _iso(hi)})
                    else:
                        raise TruncatedEnumeration(
                            f"more than {MAX_OFFSET + MAX_LIMIT} markets share end_date "
                            f"{filters.get('end_date_min')}..{filters.get('end_date_max')} and start_date "
                            f"{_iso(lo)}..{_iso(hi)}; the API offers no finer partition")
                    return
                add(last)
                self._pages(f, add)

            with lock:
                submit(_EPOCH_LO, _EPOCH_HI, "end_date", base)
            while True:
                with lock:
                    if not pending:
                        break
                    snapshot = set(pending)
                done = next(as_completed(snapshot))
                with lock:
                    pending.discard(done)
                done.result()   # re-raise GammaError / TruncatedEnumeration

        plain, _ = self._walk(**base)
        for m in plain:
            key = m.get("conditionId") or m.get("id")
            if key:
                out.setdefault(key, m)
        return out

    def _pages(self, filters: dict[str, Any], add: Callable[[list[dict[str, Any]]], None],
               batch: int = 4) -> None:
        """Fetch offsets 100..1900 of a slice known to fit under the wall, a few
        at a time, stopping at the first short page."""
        offsets = list(range(MAX_LIMIT, MAX_OFFSET, MAX_LIMIT))
        with ThreadPoolExecutor(max_workers=batch) as pool:
            for i in range(0, len(offsets), batch):
                chunk = offsets[i:i + batch]
                pages = list(pool.map(lambda o: self._get("/markets", limit=MAX_LIMIT, offset=o, **filters), chunk))
                for pg in pages:
                    add(pg)
                if any(len(pg) < MAX_LIMIT for pg in pages):
                    return

    def markets_plain(self, *, closed: bool = False) -> Iterator[dict[str, Any]]:
        """The naive walk, for comparison. Stops at the wall and says so."""
        rows, complete = self._walk(closed=_b(closed))
        yield from rows
        if not complete:
            raise TruncatedEnumeration(f"plain pagination stopped at offset {MAX_OFFSET + MAX_LIMIT}")
