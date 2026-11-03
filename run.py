"""
Paste-and-run: show the gap between what plain pagination sees and what exists.

    python3 run.py

Takes a few minutes. Prints the plain-pagination count, then the partitioned
count, then a few markets with their rules hashes.
"""
from __future__ import annotations

import gamma
from rules import fingerprint


def main() -> None:
    g = gamma.Gamma()
    plain = 0
    try:
        for _ in g.markets_plain():
            plain += 1
    except gamma.TruncatedEnumeration as e:
        print(f"plain pagination: {plain} markets, then: {e}")

    g = gamma.Gamma()
    print("partitioned enumeration running (a few hundred requests)...")
    everything = g.markets_all()
    print(f"partitioned: {len(everything)} markets in {g.requests} requests")
    print(f"plain pagination saw {100 * plain // max(len(everything), 1)}% of them\n")

    for m in list(everything.values())[:3]:
        fp = fingerprint(gamma.normalize(m))
        print(f"{fp['slug']}\n  sha256 {fp['sha256']}\n  outcomes {gamma.normalize(m).get('outcomes')}\n")


if __name__ == "__main__":
    main()
