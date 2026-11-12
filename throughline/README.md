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
python3 - <<'PYTHON'
import re
from pathlib import Path
from throughline import Throughline

values = [line.partition("=")[2].strip() for line in Path(".env").read_text().splitlines()
          if line.startswith("THROUGHLINE_API_KEY=")]
if len(values) != 1 or not re.fullmatch(r"tl_live_[A-Za-z0-9_-]{43}", values[0]):
    raise SystemExit("Expected one valid THROUGHLINE_API_KEY line; nothing was sent.")
p = Throughline(api_key=values[0]).page(limit=1)
if not p.changes:
    print("Success: no matches in this page. Empty polls count against your allowance.")
elif p.changes[0].record is None:
    print("A previously published match was withdrawn.")
else:
    record = p.changes[0].record
    print("Article:", record["title"])
    print("Contract:", record["market"]["title"])
    print("Why:", (record.get("analysis") or {}).get("why_relevant")
          or record["relevance"].get("why_relevant") or "No explanation published.")
print("Save cursor:", p.next_cursor, "More pages:", p.has_more)
PYTHON
```

The distribution is named `throughline-news-client`; the import is `throughline`.
Install from this checkout. This is not a claim that a PyPI or npm release exists.
The news client is separate from this repository's Gamma/rules archive and MCP
server; those tools do not require a Throughline account or key.

One environment variable, `THROUGHLINE_API_KEY`, is used by both clients. The
production base URL defaults to `https://api.throughline.markets`.
`Throughline(base_url, api_key)` remains available for explicit configuration.
Use HTTPS; cleartext HTTP is accepted only for local loopback development. Base
URLs cannot contain credentials, query strings or fragments.
The Python constructor reads exported environment variables. The example above
reads the single key line as data; the client does not parse `.env` itself. Never
execute/source a secret file as shell code.

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

## Command line: set up without pasting a key

`pip install .` also installs a `throughline` command (POSIX, Python 3.10+, no
dependencies). It lets an agent or script request access that **a signed-in human
approves in the browser**, so no key is ever pasted into chat.

Pairing is available at `https://api.throughline.markets`. If you prefer to
create a key yourself, choose **Copy .env line** in the account portal.

```sh
throughline login                 # prints a code and link, exits 2 (pending)
# a human opens the link, signs in, checks the code matches and approves
throughline login --wait          # agent starts this immediately after relaying code/link
throughline status                # plan and remaining allowance
throughline start-from-now        # capture the feed head once; later runs reuse it
```

`login` output (the code and link are not secrets):

```json
{"expires_in": 600, "interval": 5.0, "name": "Throughline CLI", "next": "Relay the code and link to the person, then immediately run 'throughline login --wait'.", "pairing_id": "0123456789abcdef0123456789abcdef", "pending_file": ".throughline/pending.json", "scopes": ["news:read"], "status": "pending", "user_code": "KQ4T-7MZB", "verification_uri": "https://api.throughline.markets/account", "verification_uri_complete": "https://api.throughline.markets/account#pair=KQ4T-7MZB"}
```

Every command prints exactly one JSON object on stdout and never prints a
credential. `status` is one of `ok`, `pending`, `delivered`, `denied`,
`slow_down`, `cancelled`, `delivery_unknown`, `error`. Exit codes: `0` success,
`2` pending or slow down (run again later), `3` denied or cancelled,
`4` error or delivery unknown. Errors carry a stable `error` code and fixed
fields only; response bodies are never echoed.

A denial is definitive: no keys from that request are active and the pending request is
cleared. An expired request is not. The server gives the same `expired_token`
answer for a request nobody approved and for an approved one that was not
picked up within its window, so the CLI reports `delivery_unknown` (exit `4`),
keeps `.throughline/pending.json`, and names the connection to look for. Sign in
to the account portal; if that connection is listed, revoke it. Then run
`throughline cancel` to discard the local request and `throughline login` to
start again.

How it works: the CLI generates the key locally and sends only its SHA-256 hash.
The key is unusable until a human approves the request. The secret waits in
`.throughline/pending.json`, so a lost reply or a restart loses nothing: run
`throughline login` again and it finishes the same request. After approval the
feed key is written to `.env` as `THROUGHLINE_API_KEY=...`. It does not expire;
it works until revoked.

Optional management scopes let the CLI read the account or manage keys:

```sh
throughline login --scopes account:read,keys:read,keys:write,billing:read
throughline account
throughline keys list --newest
throughline keys create --name worker --output private/worker.env
throughline keys revoke 0123456789abcdef0123456789abcdef
throughline billing status
```

The human can approve fewer scopes than requested. The management credential is
stored apart from the feed key in `.throughline/credentials.json` and expires 30
days after approval; renewing it needs a new `login --replace` and a new
approval. Revoking a connection in the portal also revokes its feed key and any
keys created through it. Listing other connections, trials and payment stay in the portal. Your own management credential can inspect and disconnect its connection.

File rules, enforced before anything is sent:

- `.env` and `.throughline/` must be git-ignored and untracked. The CLI names the
  missing `.gitignore` lines and never edits `.gitignore` itself. Outside a git
  work tree, pass `--no-git` to acknowledge that.
- Private files must be regular files owned by you with mode `0600`
  (`.throughline` is `0700`). Symlinks, hard links, other owners and group or
  world access are refused, not repaired.
- Other `.env` lines are preserved byte for byte. More than one
  `THROUGHLINE_API_KEY` line is refused. An existing key is never overwritten
  unless you pass `--replace`; the old key then stays active until revoked.
- `keys create` requires `--output`, a new ignored file inside the project. The
  key is written there and never printed. A private `.throughline/key-create.json` saves the operation before HTTP; repeat the exact command after a lost reply. Conflicting name/output operations are refused until it is resolved.
- One command at a time per project: a second `login` while one is waiting gets
  `state_locked`. `throughline cancel` drops a pending request locally; the
  unapproved request then expires on its own. `cancel` refuses if the request was
  already approved, and finishes delivery instead.
- Credentials are never accepted as arguments. The API origin is fixed;
  `THROUGHLINE_API_BASE_URL` is accepted only for loopback addresses, for tests.
  Redirects are refused, and responses are bounded in size and time.

These files keep a key out of chat, logs and source control. They do not hide it
from a shell agent doing the setup, which can read local files.

## Your plan and usage

`client.usage()` (Python) and `await client.usage()` (JavaScript) return
`{"plan": ..., "usage": {...}}` for the account that owns the key, from
`GET /v1/usage`. The call does not spend a news response; it has its own
separate limit. It needs the same server release as the command above; older
servers answer 404.

## Cursors, replay and corrections

`page()` / `page(0)` starts at the beginning of retained change history. To receive
only future publications on a server supporting `cursor=latest`, call `client.start_from_now()` in Python or
`await client.startFromNow()` in JavaScript **once**, then save that numeric
cursor and pass it to `page(cursor)`. These helpers require the Throughline foundation API release (server source
`c79aba5` or later). Older servers return 400; use numeric replay until that
release is deployed. This initial empty poll uses one request.
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
endpoint. Other 4xx responses return immediately. A deterministic `response_too_large`
503 also returns immediately; reducing the page size needs a caller decision.
Malformed successful pages raise `invalid_response` before changing saved records
or cursor. Only explicit withdrawals remove stored matches. Network failures are not automatically
retried because an admitted read may already have counted.

Errors retain `status`, `body`, `code` and parsed `details`. Additive response
fields, when supplied by the server, appear as `request_id`, `upgrade_url`,
`resets_at` in Python and `requestId`, `upgradeUrl`, `resetsAt` in JavaScript.
The backend contract may evolve; see [CHANGELOG.md](CHANGELOG.md).

## Tests

From the repository root:

```sh
python3 -I throughline/tests/test_client.py
python3 throughline/tests/test_cli.py
node --test throughline/js/tests/
```

These exercise the actual clients at their HTTP boundaries, including quota
failure, retry budgets, cancellation, saved cursors, withdrawals and replay. The
CLI tests run the real command as a subprocess in temporary git repositories
against a local HTTP server; they need `git` and never contact the hosted service.

## Agent lifecycle and safe rotation

Client0.3 adds `throughline whoami` (verified account ID, connection name, permissions, lifetime and linked key metadata), `throughline doctor` (connection and noncharged allowance checks), `throughline audit [--cursor ID]` (only this connection's retained successful access changes), and `throughline disconnect` (revoke this connection and all its keys). Feed-only login has no management token: `doctor` can still check usage; the person disconnects it in the portal.

`disconnect` clears the saved management credential after an acknowledged revoke. It removes the feed variable only when its saved hash proves that it is the key installed with that connection; it preserves other `.env` lines and unrelated files. A private disconnect receipt resumes acknowledged local cleanup. A lost server acknowledgement keeps local material and reports `disconnect_unknown`: inspect and revoke the named connection in the portal. Expired management access cannot revoke a still-active feed key; use the portal.

Key rotation uses the same recoverable issuance path: `throughline keys create --name worker-next --output private/worker-next.env` privately saves the replacement before HTTP, sends only a digest and verifies it with noncharged usage. Switch the actual consumer to that file, check its successful call, then explicitly `throughline keys revoke OLD_ID`. Creating a replacement never revokes the old key or silently rewrites another bot's configuration. Keep pending creation state on unknown outcomes. Repeating its exact command retrieves the same receipt rather than a new key; a revoked/conflicting ID never reactivates.

These commands need the matching AgentLifecycleV1 server. A404 means the deployment is older: keep local state and use the portal; do not retry by creating a different operation. New management permission still requires another human approval after30days, never silent renewal.
