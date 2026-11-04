"""
Aggregate statistics over every open market, computed from each daily sweep.

Written by watch.py to archive/stats.json and rendered to STATS.md. These are
numbers nobody can get from plain pagination, because plain pagination does not
see most of the markets.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

import gamma


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def compute(markets: dict[str, dict[str, Any]], stamp: str) -> dict[str, Any]:
    now = gamma._epoch(stamp) or 0
    by_end_month: Counter[str] = Counter()
    created_7d = created_30d = 0
    desc_len: list[int] = []
    empty_desc = 0
    empty_source = 0
    neg_risk = 0
    outcomes_n: Counter[int] = Counter()
    volume_total = 0.0
    volume_zero = 0
    volume_buckets: Counter[str] = Counter()
    ends_past = 0
    lifetime: Counter[str] = Counter()
    for m in markets.values():
        c0, e0 = gamma._epoch(m.get("createdAt") or ""), gamma._epoch(m.get("endDate") or "")
        if c0 is None or e0 is None:
            lifetime["unknown"] += 1
        else:
            d = (e0 - c0) / 86400
            lifetime["<1 day" if d < 1 else "1-7 days" if d < 7 else "7-30 days" if d < 30
                     else "30-365 days" if d < 365 else ">1 year"] += 1
        end = m.get("endDate") or ""
        by_end_month[end[:7] if end else "none"] += 1
        e = gamma._epoch(end)
        if e is not None and e < now:
            ends_past += 1
        c = gamma._epoch(m.get("createdAt") or "")
        if c is not None:
            age = now - c
            created_7d += age <= 7 * 86400
            created_30d += age <= 30 * 86400
        d = m.get("description") or ""
        desc_len.append(len(d))
        empty_desc += not d.strip()
        empty_source += not (m.get("resolutionSource") or "").strip()
        neg_risk += bool(m.get("negRisk"))
        o = gamma.normalize(m).get("outcomes")
        outcomes_n[len(o) if isinstance(o, list) else -1] += 1
        try:
            v = float(m.get("volumeNum") or m.get("volume") or 0)
        except (TypeError, ValueError):
            v = 0.0
        volume_total += v
        volume_zero += v == 0
        volume_buckets["0" if v == 0 else "<1k" if v < 1e3 else "1k-10k" if v < 1e4
                       else "10k-100k" if v < 1e5 else "100k-1M" if v < 1e6 else ">1M"] += 1
    return {
        "as_of": stamp,
        "open_markets": len(markets),
        "plain_pagination_sees": min(len(markets), gamma.MAX_OFFSET + gamma.MAX_LIMIT),
        "created_last_7d": created_7d,
        "created_last_30d": created_30d,
        "end_date_in_past_but_open": ends_past,
        "by_end_month": dict(sorted(by_end_month.items())),
        "lifetime": {k: lifetime[k] for k in ("<1 day", "1-7 days", "7-30 days", "30-365 days", ">1 year", "unknown")},
        "description_chars": {"p10": _pct(desc_len, .1), "p50": _pct(desc_len, .5),
                              "p90": _pct(desc_len, .9), "max": max(desc_len, default=0),
                              "total": sum(desc_len)},
        "empty_description": empty_desc,
        "empty_resolution_source": empty_source,
        "neg_risk": neg_risk,
        "outcomes_count": {str(k): v for k, v in sorted(outcomes_n.items())},
        "volume": {"total_usd": round(volume_total), "zero": volume_zero,
                   "buckets": {k: volume_buckets[k] for k in ("0", "<1k", "1k-10k", "10k-100k", "100k-1M", ">1M")}},
    }


def render(s: dict[str, Any], index: dict[str, Any] | None = None) -> str:
    n = s["open_markets"]
    def pc(x: int) -> str:
        return f"{100 * x / n:.1f}%" if n else "n/a"
    L = [f"# Polymarket open-market statistics", "",
         f"Computed by `watch.py` from a complete sweep of every open market on {s['as_of']}. "
         f"Regenerated daily. Source numbers in `archive/stats.json`.", "",
         "| | |", "|---|---|",
         f"| Open markets | **{n:,}** |",
         f"| Visible to plain pagination | {s['plain_pagination_sees']:,} ({pc(s['plain_pagination_sees'])}) |",
         f"| Created in the last 7 days | {s['created_last_7d']:,} |",
         f"| Created in the last 30 days | {s['created_last_30d']:,} |",
         f"| End date already passed but still open | {s['end_date_in_past_but_open']:,} |",
         f"| Empty resolution text | {s['empty_description']:,} ({pc(s['empty_description'])}) |",
         f"| Empty `resolutionSource` | {s['empty_resolution_source']:,} ({pc(s['empty_resolution_source'])}) |",
         f"| Negative-risk (multi-outcome event) markets | {s['neg_risk']:,} ({pc(s['neg_risk'])}) |",
         f"| Markets with zero volume | {s['volume']['zero']:,} ({pc(s['volume']['zero'])}) |",
         f"| Total volume (USD, as reported) | {s['volume']['total_usd']:,} |"]
    if "archived" in s:
        L += [f"| Archived (lifetime ≥ {s['min_lifetime_days']} days) | {s['archived']:,} ({pc(s['archived'])}) |"]
    if index:
        rev = sum(1 for r in index.values() if r.get("revisions", 1) > 1)
        L += [f"| Archived markets whose rules changed since the archive began | {rev:,} |"]
    L += ["", "## Market lifetime (end date minus creation)", "", "| lifetime | markets |", "|---|---|"]
    L += [f"| {k} | {v:,} |" for k, v in s["lifetime"].items()]
    L += [""]
    d = s["description_chars"]
    L += ["## Resolution text length (characters)", "",
          "| p10 | median | p90 | max | total |", "|---|---|---|---|---|",
          f"| {d['p10']:,} | {d['p50']:,} | {d['p90']:,} | {d['max']:,} | {d['total']:,} |", "",
          "## Volume distribution", "", "| bucket | markets |", "|---|---|"]
    L += [f"| {k} | {v:,} |" for k, v in s["volume"]["buckets"].items()]
    L += ["", "## Outcomes per market", "", "| outcomes | markets |", "|---|---|"]
    L += [f"| {k} | {v:,} |" for k, v in s["outcomes_count"].items()]
    L += ["", "## Open markets by end month", "", "| month | markets |", "|---|---|"]
    L += [f"| {k} | {v:,} |" for k, v in s["by_end_month"].items()]
    return "\n".join(L) + "\n"
