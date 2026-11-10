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
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator

__all__ = ["Throughline", "Page", "MatchStore", "ThroughlineError", "RateLimited"]

_UA = "throughline-python/0.1 (+https://throughline.markets/api)"
DEFAULT_BASE_URL = "https://api.throughline.markets"
_TRANSIENT = {"burst_limited", "burst_capacity", "server_capacity"}


class ThroughlineError(RuntimeError):
    """An error response from the feed."""

    def __init__(self, status: int, body: str) -> None:
        try:
            details = json.loads(body)
        except (ValueError, TypeError):
            details = {}
        self.details = details if isinstance(details, dict) else {}
        code = self.details.get("error")
        self.code = code if isinstance(code, str) else None
        self.request_id = self.details.get("request_id")
        self.upgrade_url = self.details.get("upgrade_url")
        self.resets_at = self.details.get("resets_at")
        reason = self.details.get("message") or self.code or body[:400]
        super().__init__(f"HTTP {status}: {reason}")
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

    Defaults to the production origin and ``THROUGHLINE_API_KEY``. Existing
    ``Throughline(base_url, api_key)`` calls remain supported. Only transient
    read failures retry, within ``retry_budget`` seconds overall; account quotas
    raise immediately. ``cancel`` interrupts retry waits without another call.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
        retry_budget: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        cancel: threading.Event | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        api_key = os.environ.get("THROUGHLINE_API_KEY") if api_key is None else api_key
        if not api_key:
            raise ValueError("api_key is required")
        if (not math.isfinite(timeout) or timeout <= 0 or
                not math.isfinite(retry_budget) or retry_budget < 0 or
                type(max_retries) is not int or max_retries < 0):
            raise ValueError("invalid timeout or retry policy")
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_budget = retry_budget
        self._sleep = sleep
        self._cancel = cancel

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
        started, waited = time.monotonic(), 0.0
        while True:
            if self._cancel is not None and self._cancel.is_set():
                raise InterruptedError("Throughline read cancelled")
            remaining = self._retry_budget - max(time.monotonic() - started, waited)
            if attempt and remaining <= 0:
                raise error
            try:
                timeout = min(self._timeout, remaining) if attempt else self._timeout
                with urllib.request.urlopen(req, timeout=max(0.001, timeout)) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read().decode("utf-8", "replace")
                finally:
                    exc.close()
                wait = _retry_after(exc.headers.get("Retry-After"), default=1.0 if exc.code == 503 else 5.0)
                error = RateLimited(exc.code, body, wait) if exc.code == 429 else ThroughlineError(exc.code, body)
                transient = exc.code == 503 or exc.code == 429 and (error.code is None or error.code in _TRANSIENT)
                remaining = self._retry_budget - max(time.monotonic() - started, waited)
                if transient and attempt < self._max_retries and wait < remaining:
                    # Never shorten the server's Retry-After to fit our budget.
                    if self._cancel is not None:
                        if self._cancel.wait(wait):
                            raise InterruptedError("Throughline read cancelled") from exc
                    else:
                        self._sleep(wait)
                    waited += wait
                    attempt += 1
                    continue
                raise error from exc

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
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else default
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0.0, date.timestamp() - time.time()) if date.tzinfo is not None else default
        except (ValueError, TypeError, OverflowError):
            return default
