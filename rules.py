"""
Fetch and fingerprint Polymarket resolution criteria.

Gamma stores each market's resolution rules in ``description`` and keeps no
revision history. ``updatedAt`` is a bulk batch timestamp shared across
unrelated markets, so it cannot tell you the rules changed. The only way to
know is to hash the text yourself and compare later.

    python3 rules.py https://polymarket.com/event/some-event
    python3 rules.py some-market-slug

Prints the condition ID, the SHA-256 of the rules text, and the text itself.
"""
from __future__ import annotations

import hashlib
import sys
from typing import Any

from gamma import Gamma


def rules_hash(description: str | None) -> str:
    """SHA-256 of the rules text, byte-exact.

    No whitespace normalisation. Polymarket edits are sometimes a single
    character, and a hash that ignores "small" changes cannot be used to
    prove the text you matched against was the text that was live.
    """
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


def fingerprint(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "conditionId": market.get("conditionId"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "sha256": rules_hash(market.get("description")),
        "createdAt": market.get("createdAt"),
        "endDate": market.get("endDate"),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    g = Gamma()
    m = g.market_by_slug(argv[1])
    if m is None:
        ids = g.condition_ids(argv[1])
        if not ids:
            print(f"not found: {argv[1]}")
            return 1
        print(f"event with {len(ids)} markets; pass a market slug for its rules")
        for cid in ids:
            print(f"  {cid}")
        return 0
    fp = fingerprint(m)
    print(f"conditionId  {fp['conditionId']}")
    print(f"slug         {fp['slug']}")
    print(f"question     {fp['question']}")
    print(f"sha256       {fp['sha256']}")
    print()
    print(m.get("description") or "(empty description)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
