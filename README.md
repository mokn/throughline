# Throughline

**[Throughline](https://throughline.markets) is a free, rate-limited news API
for Polymarket.** It tells you which news articles are relevant to which
prediction-market contracts, and why: the article matched to the contract, the
reason stated, the evidence it was drawn from labeled, and withdrawn matches
kept readable instead of deleted. Geopolitical markets first.

**It is not serving yet.** The analysis flow behind it runs today; the public
feed does not. [Ask to be told when it opens](mailto:access@throughline.markets?subject=Throughline%20early%20access),
or read the [proposed API](https://throughline.markets/api).

This repo is the public, working part. Everything here runs today, needs no
API key and nothing from Throughline, and will stay useful whether or not you
ever use the feed.

## The thing you can use right now

**Paginating Polymarket's Gamma API the obvious way returns under 1% of the
open markets, with HTTP 200 and no warning.**

```
plain pagination   (closed=false, walk offset until 422)      2,100 markets
partitioned        (bisect by end_date, walk every slice)   238,661 markets
```

Measured 2026-11-03 (16,670 requests). Most of the 238,661 are templated
short-lived series (hourly and daily crypto price markets, per-game sports
markets); more than 2,100 of them end on a single day. `python3 run.py`
re-measures it. [`GOTCHAS.md`](GOTCHAS.md)
has that and nine other undocumented behaviours, each with the curl probe
that produced it, and `python3 test.py --live` re-checks all of them against
the real API.

```python
from gamma import Gamma, normalize

g = Gamma()
markets = g.markets_all()          # every open market keyed by conditionId, or raises
m = normalize(markets["0x..."])    # outcomes / clobTokenIds decoded from string-JSON
g.condition_ids("https://polymarket.com/event/some-event")   # one event -> many markets
```

No dependencies. Python 3.10+. Copy `gamma.py` into your project or import it
from a checkout; there is nothing to install.

## The resolution-rules archive

Polymarket markets resolve on the text in `description`. That text can be
edited, and the API keeps no history: `updatedAt` is a batch timestamp shared
across unrelated markets, and there is no revision field. If the rules changed
after you read them, nothing tells you.

A GitHub Actions job runs [`watch.py`](watch.py) daily. It enumerates every
open market, hashes each one's rules, and commits:

| File | What |
|---|---|
| `archive/index/<shard>.json` | conditionId → slug, question, sha256, created_at, first_seen, last_changed, revisions (256 shards by the ID's last two hex chars) |
| `archive/rules/<shard>/<conditionId>.txt` | the current rules text, one file per market |
| `archive/changes/YYYY-MM-DD.md` | every edit that day, before and after |
| `archive/runs.log` | one line per run, always, so a missed day shows as a gap |
| [`STATS.md`](STATS.md) | statistics over the whole open set, regenerated each run |

Every open market is counted in the statistics, but only markets that live
seven days or longer are archived. The rest are templated hourly and daily
series (crypto price ticks, per-game sports lines) whose rules are generated
from a fixed form; archiving them would add roughly 200,000 files a day and
say nothing a single example does not. The exact number of archived versus
counted markets is on the first line of each run in `archive/runs.log`.

The archive checks itself. A market whose `createdAt` is more than two days
before the archive first saw it is listed under "late first sightings" in that
day's change file; if enumeration were missing markets, that is where it would
show. The job runs on a schedule and by hand only, never on pull requests, both
actions are pinned by commit, and the repo holds no secrets, so there is
nothing for a fork to extract. The archive began 2026-11-03 and can say nothing
about edits before that.

Throughline publishes, with every match, a hash of the rule text the match was
made against. This archive is what keeps that hash meaningful after an edit.

## Layout

| Path | What |
|---|---|
| [`GOTCHAS.md`](GOTCHAS.md) | Ten measured Gamma behaviours with probes |
| `gamma.py` | Client: partitioned enumeration, `TruncatedEnumeration` instead of a silent subset |
| `rules.py` | `python3 rules.py <market url>` prints the rules and their sha256 |
| `watch.py`, `stats.py` | The daily job and its statistics |
| `run.py` | Paste-and-run demonstration of the pagination gap |
| `test.py` | 26 offline tests against a fake that reproduces the gotchas; `--live` adds 12 against the real API |
| [`throughline/`](throughline/) | Reference clients for the Throughline feed, Python and JavaScript, encoding the published contract |
| `llms.txt` | Pointer file for coding agents |

## Throughline's contract, in brief

A match says one article is relevant to one contract. It carries the article,
the exact market plus the hash of the rules matched, the reason in words, a
novelty label (`new`, `update`, `repeat`, `uncertain`), an impact reading 0–4
that arrives only after a closer read, what evidence was actually seen (full
text, summary, headline), three separate clocks (published, received,
assessed), and a stable id with a revision so corrections replace rather than
contradict. Matches are published as `preliminary`, become `assessed` after a
closer read, or are `withdrawn` and stay readable.

Never published: forecasts, probabilities, positions, financial eligibility, or
anything that answers "is there edge against the current price". The published
set is not narrowed by any volume, price or liquidity filter. A separate
coverage endpoint distinguishes a quiet market from a broken pipeline.

The contract is [`ThroughlinePublicationV3`](throughline/schema/upsert-single.json)
and is not frozen; the [reference clients](throughline/) (Python, stdlib only;
JavaScript, no dependencies) enforce its change-stream semantics and
`throughline/CHANGELOG.md` lists breaking changes.
Full description: <https://throughline.markets/api>.

## Contributing

Found another Gamma behaviour? Open an issue with the curl probe and its
output, or a PR that adds it to `GOTCHAS.md` with a matching test in
`LiveGotchaTests`. Claims without a probe will be asked for one.

---

Not affiliated with, endorsed by, or operated by Polymarket. "Polymarket" is
used only to identify the venue whose markets are covered. Throughline does
not forecast outcomes, does not say what to trade, and is not investment
advice. MIT licensed.
