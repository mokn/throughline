"""
Polymarket Gamma API client that does not return partial answers that look complete.

Gamma (https://gamma-api.polymarket.com) is the public, unauthenticated read API
for Polymarket markets. Measured against the live API on 2026-11-03 it does the
following, none of which is documented:

1. ``limit`` is silently clamped to 100. Ask for 500, get 100 rows and HTTP 200.
2. ``offset`` returns HTTP 422 above roughly 2000, and adding filters does not
   move the wall. Plain pagination therefore stops at ~2100 markets.
3. Those ~2100 are about a quarter of the open markets. Partitioning the walk
   by ``end_date_min``/``end_date_max`` found 8400+ distinct open markets.
4. Unknown query parameters are ignored, not rejected. ``id_min=…`` returns
   HTTP 200 and the unfiltered result set.
5. ``outcomes``, ``outcomePrices``, ``clobTokenIds`` and ``umaResolutionStatuses``
   are JSON-encoded *strings*, not arrays.
6. ``updatedAt`` is a bulk batch timestamp shared across unrelated markets.
   It cannot tell you that a market's rules changed.
7. ``description`` holds the resolution criteria with no revision history.

The guiding rule here is: never return a partial answer that looks complete.
Enumeration either covers every reachable market or raises.

Stdlib only. No credentials. See GOTCHAS.md for the probes behind each claim.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
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
    def __init__(self, fetch: Fetch | None = None, base: str = BASE, retries: int = 4) -> None:
        self._fetch = fetch or self._http
        self._base = base.rstrip("/")
        self._retries = retries
        self.requests = 0

    # -- transport -----------------------------------------------------------

    @staticmethod
    def _http(url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-gamma-tools/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{self._base}{path}?{urllib.parse.urlencode(clean)}" if clean else f"{self._base}{path}"
        for attempt in range(self._retries + 1):
            self.requests += 1
            status, body = self._fetch(url)
            if status == 200:
                return json.loads(body)
            if status == 429 or status >= 500:
                if attempt < self._retries:
                    time.sleep(2 ** attempt)
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

    def _partition(self, lo: int, hi: int, axis: str, out: dict[str, dict[str, Any]], **filters: Any) -> None:
        rows, complete = self._walk(**{f"{axis}_min": _iso(lo), f"{axis}_max": _iso(hi)}, **filters)
        for m in rows:
            key = m.get("conditionId") or m.get("id")
            if key:
                out[key] = m
        if complete:
            return
        if hi - lo > _MIN_SLICE_SECONDS:
            mid = (lo + hi) // 2
            self._partition(lo, mid, axis, out, **filters)
            self._partition(mid + 1, hi, axis, out, **filters)   # both bounds inclusive
            return
        if axis == "end_date":
            # One minute of end dates still holds >2000 markets: slice by start date inside it.
            self._partition(_EPOCH_LO, _EPOCH_HI, "start_date", out,
                            end_date_min=_iso(lo), end_date_max=_iso(hi), **filters)
            return
        raise TruncatedEnumeration(
            f"more than {MAX_OFFSET + MAX_LIMIT} markets share end_date "
            f"{filters.get('end_date_min')}..{filters.get('end_date_max')} and start_date "
            f"{_iso(lo)}..{_iso(hi)}; the API offers no finer partition"
        )

    def markets_all(self, *, closed: bool = False) -> dict[str, dict[str, Any]]:
        """Every reachable market, keyed by condition ID, or raise.

        Partitions the end-date axis by bisection until every slice exhausts
        below the offset wall, then unions a plain walk of the first 2000 so
        markets without an end date (which date filters cannot reach) are
        included when they sort early. Raises TruncatedEnumeration rather than
        returning a subset.
        """
        out: dict[str, dict[str, Any]] = {}
        self._partition(_EPOCH_LO, _EPOCH_HI, "end_date", out, closed=_b(closed))
        plain, _ = self._walk(closed=_b(closed))
        for m in plain:
            key = m.get("conditionId") or m.get("id")
            if key:
                out.setdefault(key, m)
        return out

    def markets_plain(self, *, closed: bool = False) -> Iterator[dict[str, Any]]:
        """The naive walk, for comparison. Stops at the wall and says so."""
        rows, complete = self._walk(closed=_b(closed))
        yield from rows
        if not complete:
            raise TruncatedEnumeration(f"plain pagination stopped at offset {MAX_OFFSET + MAX_LIMIT}")
