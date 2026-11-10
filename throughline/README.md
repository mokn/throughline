# Throughline clients

Throughline is a news relevance API for Polymarket. Public testing is open at
[throughline.markets](https://throughline.markets). Sign in at
[the account portal](https://api.throughline.markets/account), create a key and
save **Copy .env line** to a private `.env` file. Keep it out of source control
and chat. No key is issued before verified sign-in.

[API reference](https://throughline.markets/api) ·
[Agent instructions](https://throughline.markets/agents.md) ·
[Machine-readable contract](https://throughline.markets/openapi.json)

## Install and make a first request

The Python client needs Python 3.10+ and has no runtime dependencies. From this
repository's root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
set -a
. ./.env
set +a
python3 -c 'from throughline import Throughline; p = Throughline().page(limit=1); print(len(p.changes), p.next_cursor, p.has_more)'
```

The distribution is named `throughline-news-client`; the import is `throughline`.
Install from this checkout. This is not a claim that a PyPI or npm release exists.
The news client is separate from this repository's Gamma/rules archive and MCP
server; those tools do not require a Throughline account or key.

One environment variable, `THROUGHLINE_API_KEY`, is used by both clients. The
production base URL defaults to `https://api.throughline.markets`.
`Throughline(base_url, api_key)` remains available for explicit configuration.
Python reads exported environment variables; it does not parse `.env` itself.

For Node 20.6+ with a private `.env` file, create `first-request.mjs` at the
repository root:

```js
import { Throughline } from "./throughline/js/throughline.mjs";
const page = await new Throughline().page(0, { limit: 1 });
console.log(page.changes.length, page.nextCursor, page.hasMore);
```

Run `node --env-file=.env first-request.mjs`. On Node 18+, export the variable
first and run `node first-request.mjs`. The JavaScript client has no dependencies
and ships TypeScript declarations. Its optional constructor is
`new Throughline(baseUrl, apiKey, options)`. Browser callers must provide a key
explicitly; never put a private key in a public/shared frontend.

## Cursors, replay and corrections

`page()` / `page(0)` starts at the beginning of retained change history. To receive
only future publications, call `client.start_from_now()` in Python or
`await client.startFromNow()` in JavaScript **once**, then save that numeric
cursor and pass it to `page(cursor)`. This initial empty poll uses one request.
Do not repeat the head capture on restart: doing so skips intervening changes.

Follow `next_cursor` (`nextCursor` in JavaScript), including empty filtered pages.
When `has_more` is true, fetch another page. Persist the applied records and their
cursor in the same transaction. A crash before saving both must replay the page.
A cursor is an ordering token, not a timestamp or a retention guarantee.

An `upsert` replaces the entire current record identified by
`(stream_id, record_id)`. A withdrawal has `record: null`; it is never inferred
from absence and a later upsert may reinstate the match. `MatchStore` is an
in-memory example that handles this identity, replacement and idempotent replay;
it is not durable storage for a production integration.

## Limits and errors

Free is 100 successful news responses per account per UTC day. The explicit
seven-day, no-card trial is 5,000/day; Builder is $29/month for 5,000/day.
All keys share usage. Empty polls count. Coverage checks have a separate bounded
allowance. Follow the response's `X-RateLimit-*` headers; limits are snapshots,
not capacity reservations. One poll every 15 minutes is 96/day before catch-up
pages; every 30 seconds is 2,880/day.

`ThroughlineError.code` reads the response's **`error`** field. Account quota/data
errors raise `RateLimited` immediately, even if `Retry-After` is short. Present
its reason and any `upgrade_url` to the human; payment stays human. Do not retry
an account allowance error in a tight loop or rotate keys to evade it.

Transient 503 and burst/server-capacity 429 responses can retry within a default
60-second total retry budget and five retries. A longer `Retry-After` is returned
to the caller, never shortened. Configure `retry_budget` (Python) or `retryBudget`
(JavaScript) in seconds. Python accepts a `threading.Event` as `cancel` to
interrupt retry waits; JavaScript accepts an `AbortSignal` as `signal` for waits
and fetches. Python's active blocking HTTP request still uses its request timeout.
Redirects are refused so bearer credentials cannot be forwarded to a different
endpoint. Other 4xx responses return immediately. Network failures are not automatically
retried because an admitted read may already have counted.

Errors retain `status`, `body`, `code` and parsed `details`. Additive response
fields, when supplied by the server, appear as `request_id`, `upgrade_url`,
`resets_at` in Python and `requestId`, `upgradeUrl`, `resetsAt` in JavaScript.
The backend contract may evolve; see [CHANGELOG.md](CHANGELOG.md).

## Tests

From the repository root:

```sh
python3 -I throughline/tests/test_client.py
node --test throughline/js/tests/
```

These exercise the actual clients at their HTTP boundaries, including quota
failure, retry budgets, cancellation, saved cursors, withdrawals and replay.
