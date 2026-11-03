# Gamma API gotchas

Everything below was measured against `https://gamma-api.polymarket.com` on
2026-11-03. Each item has the probe that produced it, so you can re-run it
instead of trusting it. `python3 test.py --live` runs them all as tests.

None of this is documented by Polymarket. If any of it stops being true the
live tests fail and this file is wrong.

## 1. `limit` is silently clamped to 100

Ask for 500, get 100 rows and HTTP 200. Nothing in the response says you were
clamped.

```
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=500' | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))'
100
```

The failure this causes: a client that validates `limit <= 1000`, asks for
500, gets 100, sees `100 < 500` and concludes the result set is exhausted. It
returns 100 markets and exits cleanly. At least one published SDK does exactly
this.

## 2. `offset` returns HTTP 422 above 2000

```
$ curl -s -o /dev/null -w '%{http_code}\n' 'https://gamma-api.polymarket.com/markets?limit=100&offset=2000&closed=false'
200
$ curl -s -o /dev/null -w '%{http_code}\n' 'https://gamma-api.polymarket.com/markets?limit=100&offset=2100&closed=false'
422
```

Adding filters does not move the wall. `end_date_min=…&offset=2500` is still 422.
Plain pagination therefore tops out at 2,100 rows.

## 3. Plain pagination sees under 1% of open markets

Walking `closed=false` with `limit=100` from offset 0 until the 422 yields
2,100 markets. Partitioning the same query by `end_date_min`/`end_date_max`
and walking each slice to exhaustion yields **238,661** distinct open markets
(measured 2026-11-03, 16,670 requests). `gamma.Gamma.markets_all()` bisects
the end-date axis until every slice exhausts below the wall; `run.py` prints
both numbers side by side.

The population is dominated by templated short-lived series. More than 2,100
markets end on 2026-11-04 alone:

```
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=100&closed=false&end_date_min=2026-11-04T00:00:00Z&end_date_max=2026-11-04T23:59:59Z&offset=2000' | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))'
100
```

A full page at offset 2000 inside a one-day slice means that slice alone
exceeds the wall; the client then bisects by hour, minute, and finally by
`start_date`.

Date filters are real filters (unlike item 4), and both bounds are inclusive:

```
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=3&closed=false' | python3 -c 'import json,sys;[print(m["id"],m["endDate"]) for m in json.load(sys.stdin)]'
559651 2027-01-01T04:59:00Z
559652 2028-11-08T12:00:00Z
559653 2028-11-08T12:00:00Z
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=3&closed=false&end_date_max=2026-12-01T00:00:00Z' | python3 -c 'import json,sys;[print(m["id"],m["endDate"]) for m in json.load(sys.stdin)]'
562793 2026-11-04T04:59:00Z
562794 2026-11-04T04:59:00Z
562802 2026-11-04T04:59:00Z
```

`start_date_min`/`start_date_max` also filter, and `markets_all()` falls back
to them if a single end-date instant ever holds more than 2,100 markets.
Markets with no `endDate` cannot be reached by date filters; `markets_all()`
unions the first 2,000 rows of a plain walk so those are included when they
sort early, and documents that the rest are unreachable.

## 4. Unknown query parameters are ignored, not rejected

```
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=3&closed=false&id_min=999999999' | python3 -c 'import json,sys;[print(m["id"]) for m in json.load(sys.stdin)]'
559651
559652
559653
```

Same three rows as with no filter. A typo in a parameter name returns the
unfiltered set with HTTP 200. The parameters that are known to do something:
`limit, offset, active, closed, slug, tag_id, order, ascending,
end_date_min, end_date_max, start_date_min, start_date_max`.

## 5. Four fields are JSON-encoded strings

`outcomes`, `outcomePrices`, `clobTokenIds` and `umaResolutionStatuses` arrive
as strings containing JSON, not as arrays:

```
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=1' | python3 -c 'import json,sys;m=json.load(sys.stdin)[0];print(repr(m["outcomes"]), repr(m["clobTokenIds"])[:40])'
'["Yes", "No"]' '["3233822019007135143577280177972530224
```

`gamma.normalize(market)` decodes all four and leaves anything that is not
valid JSON untouched, so an upstream format change shows up as a string rather
than as an empty list.

## 6. `updatedAt` is a bulk batch timestamp

Unrelated markets share `updatedAt` to the microsecond. It records when a
batch job last touched the row, not when the market's content changed.

```
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=100&closed=false&offset=1000' | python3 -c 'import json,sys;r=json.load(sys.stdin);s={m["updatedAt"] for m in r};print(len(r),"rows,",len(s),"distinct updatedAt")'
100 rows, 9 distinct updatedAt
```

You cannot use it to detect a rules edit. Hash `description` yourself
(`rules.py`), or use the archive in this repo.

## 7. Resolution rules have no revision history

`description` holds the resolution criteria. There is no field for a previous
version, no edit timestamp specific to the text, and `resolutionSource` is
usually empty. If the rules were edited after you read them, the API does not
tell you. `archive/` in this repo is a daily snapshot that does.

## 8. One event URL fans out to many markets

A `polymarket.com/event/<slug>` URL is not one market. The 2028 Democratic
nominee event, for example, is 128 markets, each with its own `conditionId`.
`gamma.Gamma.condition_ids(url)` returns all of them; `market_by_slug` returns
the single market or `None`.

## 9. `closed=false` means unresolved, not "ends in the future"

The open set includes markets whose end date passed months ago and that are
simply waiting on resolution. `umaResolutionStatuses` is `[]` for them.

```
$ curl -s 'https://gamma-api.polymarket.com/markets?limit=2&closed=false&end_date_max=2026-06-01T00:00:00Z' | python3 -c 'import json,sys;[print(m["id"],m["endDate"],m["closed"],m["umaResolutionStatuses"]) for m in json.load(sys.stdin)]'
536934 2025-11-24T04:59:00Z False []
536935 2025-11-24T04:59:00Z False []
```

The earliest end date among open markets on 2026-11-03 was 2025-11-24. If you
want markets that are still tradeable, filter on `endDate` yourself; the
archive's STATS.md reports how many open markets have an end date in the past.

## 10. `active` and `archived` do not filter `/markets`

`closed` is honoured (item 3). `active=true`, `active=false` and
`archived=true` all return the same first page as no filter at all:

```
$ for q in '' '&active=true' '&active=false' '&archived=true'; do curl -s "https://gamma-api.polymarket.com/markets?limit=5&closed=false$q" | python3 -c 'import json,sys;print([m["id"] for m in json.load(sys.stdin)])'; done
['559651', '559652', '559653', '559654', '559655']
['559651', '559652', '559653', '559654', '559655']
['559651', '559652', '559653', '559654', '559655']
['559651', '559652', '559653', '559654', '559655']
```

Every row in the open set carries `active: true, archived: false`, so there
is nothing for those filters to remove; they are not a way to narrow the sweep.
