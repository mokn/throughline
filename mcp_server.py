"""
An MCP server over this repository: Polymarket market lookup, resolution
rules with their hash, archived rule history, hash verification, archive
search, recent rule edits and the open-market statistics, as tools an agent
can call.

Stdlib only. Speaks the Model Context Protocol over stdio: one JSON-RPC 2.0
message per line on stdin, one per line on stdout, logs on stderr. Register it
with whatever runs your agent:

    claude mcp add throughline -- python3 /path/to/throughline/mcp_server.py

    # Cursor, Claude Desktop, Windsurf, Zed: the same command in their JSON
    {"mcpServers": {"throughline": {"command": "python3",
                                    "args": ["/path/to/throughline/mcp_server.py"]}}}

Two tools hit gamma-api.polymarket.com (``market``, ``event_markets``); the
rest read the archive in this checkout and never touch the network. The
archive covers markets that live 30 days or longer; ``git pull`` keeps it
current.

    python3 mcp_server.py            # serve on stdio
    python3 mcp_server.py --tools    # print the tool list and exit
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

import feed
import gamma
import history
import watch
from rules import rules_hash

NAME = "throughline"
VERSION = "0.1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Tools over Polymarket's Gamma API and a daily archive of resolution rules. "
    "`market` and `event_markets` query the live API. The others read the archive, which "
    "covers open markets that live 30 days or longer and keeps every revision of their "
    "rules text with a sha256 per revision. Resolution rules are the `description` field; "
    "Polymarket keeps no revision history of them, this archive does. "
    "Plain pagination of the Gamma API returns under 1% of open markets; use the "
    "partitioned client in gamma.py, not offset walking, to enumerate."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "market",
        "description": (
            "Look up one Polymarket market on the live Gamma API by market URL, slug or "
            "conditionId. Returns its question, outcomes and prices, dates, status, the "
            "current resolution rules text and the sha256 of that text, and whether the "
            "archive tracks it. For an event URL (many markets) use event_markets."),
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string",
                                   "description": "Market URL, slug or 0x… conditionId"}},
            "required": ["ref"],
        },
    },
    {
        "name": "event_markets",
        "description": (
            "List every market under a Polymarket event, from the live Gamma API, by event "
            "URL or slug: conditionId, slug, question, closed, end date. A presidential "
            "nominee event has over a hundred."),
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "Event URL or slug"}},
            "required": ["ref"],
        },
    },
    {
        "name": "rules_history",
        "description": (
            "Every archived revision of a market's resolution rules, oldest first, each with "
            "the date the watcher first saw it, its sha256 and its full text, plus the "
            "archive's index record. Offline. 'not archived' means the market lives under 30 "
            "days, closed before the archive began (2026-11-03), or has not been seen yet."),
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string",
                                   "description": "Market URL, slug or 0x… conditionId"}},
            "required": ["ref"],
        },
    },
    {
        "name": "verify_rules_hash",
        "description": (
            "Say whether a sha256 matches any archived revision of a market's rules, and "
            "when that text was live. Use it to check a rules hash you recorded earlier, or "
            "one Throughline attached to a match. Offline."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Market URL, slug or 0x… conditionId"},
                "sha256": {"type": "string", "description": "64 hex characters"},
            },
            "required": ["ref", "sha256"],
        },
    },
    {
        "name": "search_archive",
        "description": (
            "Case-insensitive substring search over the question and slug of every archived "
            "market (open, lifetime 30 days or longer). Offline, instant. Returns conditionId, "
            "slug, question, revision count, last change and URL. Not a search of all of "
            "Polymarket: short-lived templated markets are not archived."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recent_rule_changes",
        "description": (
            "The most recent edits to any archived market's resolution rules, newest first, "
            "each with the market, revision, hashes before and after, and a unified diff. "
            "Offline. Empty until the watcher sees its first edit."),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}},
        },
    },
    {
        "name": "archive_stats",
        "description": (
            "Statistics from the latest complete sweep of every open Polymarket market: how "
            "many exist, how few plain pagination sees, lifetimes, empty rules, volume, and "
            "the archive's own run log line. Offline."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    def __init__(self, gamma_factory: Callable[[], gamma.Gamma] | None = None,
                 root: Path | None = None) -> None:
        self._gamma_factory = gamma_factory or gamma.Gamma
        self._gamma: gamma.Gamma | None = None
        self.root = root or watch.ROOT
        self._index: dict | None = None

    # -- JSON-RPC ----------------------------------------------------------

    def handle(self, msg: Any) -> dict | None:
        """One decoded message in, one response out (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            want = params.get("protocolVersion")
            return _result(mid, {
                "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": NAME, "version": VERSION},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = getattr(self, f"tool_{name}", None) if name in {t["name"] for t in TOOLS} else None
            if tool is None:
                return _error(mid, -32602, f"unknown tool: {name}")
            try:
                data = tool(**args)
            except TypeError as e:
                return _error(mid, -32602, f"bad arguments for {name}: {e}")
            except gamma.GammaError as e:
                return _result(mid, _tool_error(f"Gamma API error: {e}"))
            except Exception as e:  # a tool failure is a tool result, not a protocol error
                traceback.print_exc(file=sys.stderr)
                return _result(mid, _tool_error(f"{type(e).__name__}: {e}"))
            return _result(mid, {
                "content": [{"type": "text", "text": json.dumps(data, indent=1, ensure_ascii=False)}],
                "structuredContent": data if isinstance(data, dict) else {"result": data},
                "isError": False,
            })
        return _error(mid, -32601, f"method not found: {method}")

    def serve(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply: dict | None = _error(None, -32700, "parse error")
            else:
                reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()

    # -- tools -------------------------------------------------------------

    def tool_market(self, ref: str) -> dict:
        m = self.gamma.market(ref)
        if m is None:
            ids = self.gamma.condition_ids(ref)
            if ids:
                return {"found": False, "note": f"that is an event with {len(ids)} markets; "
                                               "call event_markets", "conditionIds": ids}
            return {"found": False, "ref": ref}
        n = gamma.normalize(m)
        cid = m.get("conditionId")
        rec = self.index.get(cid) if cid else None
        return {
            "found": True,
            "conditionId": cid,
            "slug": m.get("slug"),
            "url": f"https://polymarket.com/market/{m.get('slug')}",
            "question": m.get("question"),
            "outcomes": n.get("outcomes"),
            "outcomePrices": n.get("outcomePrices"),
            "createdAt": m.get("createdAt"),
            "startDate": m.get("startDate"),
            "endDate": m.get("endDate"),
            "closed": m.get("closed"),
            "volume": m.get("volumeNum", m.get("volume")),
            "negRisk": m.get("negRisk"),
            "resolutionSource": m.get("resolutionSource"),
            "rules_sha256": rules_hash(m.get("description")),
            "rules": m.get("description") or "",
            "archived": rec is not None,
            "archive": rec,
            "rules_changed_since_archived": bool(rec) and rec.get("sha256") != rules_hash(m.get("description")),
        }

    def tool_event_markets(self, ref: str) -> dict:
        ev = self.gamma.event(ref)
        if ev is None:
            return {"found": False, "ref": ref}
        return {
            "found": True,
            "slug": ev.get("slug"),
            "title": ev.get("title"),
            "markets": [{"conditionId": m.get("conditionId"), "slug": m.get("slug"),
                         "question": m.get("question"), "closed": m.get("closed"),
                         "endDate": m.get("endDate")} for m in ev.get("markets", [])],
        }

    def tool_rules_history(self, ref: str) -> dict:
        hit = history.find(ref, self.index)
        if hit is None:
            return {"archived": False, "ref": ref,
                    "note": "not archived: lifetime under 30 days, closed before 2026-11-03, or not seen yet"}
        cid, rec = hit
        revs = history.revisions(cid, self.root)
        return {"archived": True, "conditionId": cid, "index": rec,
                "url": f"https://polymarket.com/market/{rec.get('slug')}", "revisions": revs}

    def tool_verify_rules_hash(self, ref: str, sha256: str) -> dict:
        digest = sha256.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise TypeError("sha256 must be 64 hex characters")
        hit = history.find(ref, self.index)
        if hit is None:
            return {"archived": False, "ref": ref, "match": False}
        cid, rec = hit
        matches = history.verify(cid, digest, self.root)
        return {"archived": True, "conditionId": cid, "match": bool(matches),
                "current": rec.get("sha256") == digest,
                "matches": [{k: v for k, v in r.items() if k != "text"} for r in matches]}

    def tool_search_archive(self, query: str, limit: int = 20) -> dict:
        q = query.strip().lower()
        if not q:
            raise TypeError("query must not be empty")
        limit = max(1, min(int(limit), 200))
        hits = []
        for cid, rec in self.index.items():
            if q in (rec.get("question") or "").lower() or q in (rec.get("slug") or "").lower():
                hits.append({"conditionId": cid, "slug": rec.get("slug"), "question": rec.get("question"),
                             "revisions": rec.get("revisions", 1), "last_changed": rec.get("last_changed"),
                             "still_open": "last_seen_open" not in rec,
                             "url": f"https://polymarket.com/market/{rec.get('slug')}"})
        hits.sort(key=lambda h: (h["slug"] or ""))
        return {"query": query, "total": len(hits), "returned": min(len(hits), limit), "markets": hits[:limit]}

    def tool_recent_rule_changes(self, limit: int = 20) -> dict:
        limit = max(1, min(int(limit), 200))
        recs = feed.load(self.root / "archive" / "changes", limit)
        return {"changes": [{
            "conditionId": r["conditionId"], "slug": r.get("slug"), "question": r.get("question"),
            "url": f"https://polymarket.com/market/{r.get('slug')}", "revision": r.get("revision"),
            "seen": r["seen"], "sha256_before": r.get("sha256_before"), "sha256_after": r.get("sha256_after"),
            "diff": feed.unified_diff(r.get("before") or "", r.get("after") or "", r["conditionId"]),
        } for r in recs], "feed": feed.FEED_URL}

    def tool_archive_stats(self) -> dict:
        path = self.root / "archive" / "stats.json"
        runs = self.root / "archive" / "runs.log"
        if not path.exists():
            return {"available": False, "note": "no archive/stats.json in this checkout"}
        st = json.loads(path.read_text())
        lines = runs.read_text().splitlines() if runs.exists() else []
        return {"available": True, "stats": st, "last_run": lines[-1] if lines else None,
                "runs": len(lines), "archived_markets": len(self.index),
                "note": "plain pagination of the Gamma API sees about 2,100 of these; see GOTCHAS.md"}

    # -- lazy resources ----------------------------------------------------

    @property
    def gamma(self) -> gamma.Gamma:
        if self._gamma is None:
            self._gamma = self._gamma_factory()
        return self._gamma

    @property
    def index(self) -> dict:
        if self._index is None:
            self._index = watch._load_index()
        return self._index


def _result(mid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def main(argv: list[str]) -> int:
    if "--tools" in argv:
        for t in TOOLS:
            print(f"{t['name']}: {t['description']}\n")
        return 0
    Server().serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
