"""Reference client for the Throughline news relevance feed.

Stdlib only, on purpose: an MCP server and a cron job should not inherit a
dependency tree to read a feed.

The feed is a change stream, not a list. Three things are easy to get wrong and
all three are handled here, because getting them wrong leaves wrong data in your
copy forever:

1. An ``upsert`` is a COMPLETE REPLACEMENT of that record, not a patch.
2. A withdrawal arrives as ``record: null``. It is never inferred from absence,
   and a later upsert may reinstate the same match.
3. The cursor must be persisted TOGETHER WITH the records it brought, and never
   advanced before they are saved. Otherwise a crash between the two skips a
   change permanently.

A match is identified by the ``(stream_id, record_id)`` pair, not by
``record_id`` alone.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

__all__ = ["Throughline", "Page", "MatchStore", "ThroughlineError", "RateLimited"]

_UA = "throughline-python/0.1 (+https://throughline.markets/api)"


class ThroughlineError(RuntimeError):
    """An error response from the feed."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class RateLimited(ThroughlineError):
    """429. ``retry_after`` is in seconds."""

    def __init__(self, status: int, body: str, retry_after: float) -> None:
        super().__init__(status, body)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Change:
    """One change from the stream."""

    cursor: int
    stream_id: str
    record_id: str
    operation: str
    record: dict[str, Any] | None
    schema: str

    @property
    def key(self) -> tuple[str, str]:
        """The identity of the match this change applies to."""
        return (self.stream_id, self.record_id)

    @property
    def is_withdrawal(self) -> bool:
        """True when this change removes the match from current views.

        Note this is driven by ``record is None`` rather than by the operation
        name, so an unrecognised operation that carries no record is still
        treated as a removal rather than silently ignored.
        """
        return self.record is None


@dataclass(frozen=True)
class Page:
    changes: list[Change]
    next_cursor: int
    has_more: bool


class Throughline:
    """A read-only client for the Throughline feed.

    ``base_url`` has no default: the public origin is assigned at launch, so a
    guessed hostname would fail confusingly rather than loudly.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep

    # -- transport ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None}
        )
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.code == 429:
                    wait = _retry_after(exc.headers.get("Retry-After"))
                    attempt += 1
                    if attempt > self._max_retries:
                        raise RateLimited(exc.code, body, wait) from exc
                    # Honour the server's own number. Do not invent a backoff
                    # that is faster than what it asked for.
                    self._sleep(wait)
                    continue
                raise ThroughlineError(exc.code, body) from exc

    # -- reads --------------------------------------------------------------

    def page(
        self,
        cursor: int = 0,
        *,
        limit: int | None = None,
        market_id: str | None = None,
    ) -> Page:
        """One page of changes at or after ``cursor``, oldest first.

        ``cursor=0`` is the start of retained history. There is no snapshot
        endpoint, so a new consumer replays from zero.

        ``market_id`` is an exact condition ID -- not a slug, event ID or token
        ID.
        """
        payload = self._get(
            "/v1/news",
            {"cursor": cursor, "limit": limit, "market_id": market_id},
        )
        return _parse_page(payload, fallback_cursor=cursor)

    def match(self, record_id: str, *, stream_id: str | None = None) -> dict[str, Any] | None:
        """The current head of one match, or ``None`` if it is withdrawn.

        Pass ``stream_id`` to disambiguate; without it a shared record ID
        returns 409.
        """
        payload = self._get(f"/v1/news/{urllib.parse.quote(record_id)}", {"stream_id": stream_id})
        envelope = payload.get("envelope", payload)
        return envelope.get("record")

    def coverage(self) -> dict[str, Any]:
        """Supplied coverage and status facts, with unknowns left explicit.

        Read this as well as the feed. A quiet feed and a broken pipeline look
        identical from ``/v1/news`` alone, and a market's last match is not its
        last screen.
        """
        return self._get("/v1/coverage")

    # -- streaming ----------------------------------------------------------

    def changes(
        self,
        cursor: int = 0,
        *,
        limit: int | None = None,
        market_id: str | None = None,
    ) -> Iterator[Page]:
        """Yield pages until the head is reached.

        Yields whole pages rather than individual changes so the caller can
        save records and cursor together in one transaction. Advancing the
        cursor before the records are durable is the one mistake this client
        cannot make on your behalf.
        """
        while True:
            page = self.page(cursor, limit=limit, market_id=market_id)
            yield page
            # Even an empty filtered page can advance the cursor.
            cursor = page.next_cursor
            if not page.has_more:
                return


class MatchStore:
    """An in-memory current view, applying the stream correctly.

    Swap this for your own persistence. The point of it is the ``apply``
    semantics: replacement rather than merge, removal on withdrawal,
    reinstatement on a later upsert, and idempotence on replay.
    """

    def __init__(self) -> None:
        self._current: dict[tuple[str, str], dict[str, Any]] = {}
        self._cursor = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    def apply(self, page: Page) -> None:
        """Apply a page, then adopt its cursor. Safe to replay."""
        for change in page.changes:
            if change.cursor <= self._cursor:
                continue  # already applied; replay is free
            if change.is_withdrawal:
                self._current.pop(change.key, None)
            else:
                # Complete replacement, never a merge.
                self._current[change.key] = change.record  # type: ignore[assignment]
        self._cursor = max(self._cursor, page.next_cursor)

    def current(self) -> list[dict[str, Any]]:
        return list(self._current.values())

    def for_market(self, condition_id: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self._current.values()
            if (r.get("market") or {}).get("condition_id") == condition_id
        ]

    def __len__(self) -> int:
        return len(self._current)


# -- parsing ----------------------------------------------------------------


def _parse_page(payload: dict[str, Any], *, fallback_cursor: int) -> Page:
    changes: list[Change] = []
    for item in payload.get("items") or []:
        envelope = item.get("envelope") or {}
        changes.append(
            Change(
                cursor=int(item.get("cursor", 0)),
                stream_id=str(envelope.get("stream_id", "")),
                record_id=str(envelope.get("record_id", "")),
                operation=str(envelope.get("operation", "")),
                record=envelope.get("record"),
                schema=str(envelope.get("schema", "")),
            )
        )
    next_cursor = payload.get("next_cursor")
    return Page(
        changes=changes,
        # A missing next_cursor must not silently reset the caller to zero.
        next_cursor=int(next_cursor) if next_cursor is not None else fallback_cursor,
        has_more=bool(payload.get("has_more", False)),
    )


def _retry_after(value: str | None, *, default: float = 5.0) -> float:
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        return default
