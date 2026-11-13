# Changelog

## 0.3.0 — 2026-11-13

- Added `whoami`, `doctor`, `audit` and own-connection `disconnect`. Doctor
  checks access and usage without spending a news response. Existing scopes
  still apply; payments and renewed consent remain with the person.
- Key creation privately persists the secret before sending its hash and
  operation ID. An identical retry recovers the original receipt after a lost
  reply. Rotation keeps the old key active until the consumer switches and the
  operator explicitly revokes it.
- `keys pending --park` retains private recovery material while freeing the
  active operation slot; `--resume ID` restores it. Parked secrets do not expire.
- Disconnect saves intent before DELETE. Unknown acknowledgements retain local
  material and report `disconnect_unknown`; expired management access does not
  prove linked feed keys were revoked. Only an acknowledged revoke cleans up
  positively identified local credentials. See the README for portal recovery.
- Published on GitHub for the installed AgentLifecycleV1 service. No registry
  release or new runtime dependency.

## 0.2.0 — 2026-11-10

- Added the `throughline` command (version 0.2.0): delegated setup where a
  signed-in human approves a locally generated key, private-file storage with
  git-ignore, symlink, owner and mode checks, resumable pending state, account,
  key and billing reads under optional scopes, and a once-only start-from-now
  cursor. The hosted API supports the pairing and usage routes.
- `throughline login` no longer treats an expired request as proof that nothing
  was activated. It reports `delivery_unknown` (exit 4), keeps the pending
  secrets and names the connection to inspect or revoke in the portal. Denial
  stays definitive. The `expired` status is gone.
- `throughline start-from-now` reads the head through the command's own bounded
  request path: one attempt, one whole-request deadline, no redirect. A reply
  that is not an empty page with a numeric cursor is `invalid_response` and
  saves nothing. A saved cursor is still returned without a second capture.
- A denied request now says "No keys from this request are active."

- Both feed clients gain a usage reader (`client.usage()` in Python and
  JavaScript) for the account's plan and current usage from `GET /v1/usage`. It
  does not spend a news response and has its own separate limit. It needs the
  same server release as the command. Quota errors expose `reset_at`
  (`reset_at` in Python, `resetAt` in JavaScript); the older `resets_at` and
  `resetsAt` attributes remain.

- Require HTTPS base URLs except explicit loopback development; reject credentials, queries and fragments in the base URL.

- Reject malformed pages, cursors and absent upsert records before they can corrupt saved state. Return deterministic response_too_large without retrying.

- Reject whitespace, control characters and non-ASCII credentials before transport without echoing the key in an exception.
- Both clients can capture the current hosted feed cursor before polling.

- Refuse authenticated HTTP redirects in both clients; Python no longer forwards a bearer key through urllib redirects.

- Added explicit start-from-now cursor helpers; save the numeric result before polling.
- Added local Python packaging as `throughline-news-client` and consistent production setup instructions. No registry release.

- Quota/data-cap errors raise immediately with machine-readable error and recovery
  fields. Transient read failures alone retry, respecting numeric/HTTP-date
  `Retry-After`, a configurable 60-second retry budget and cancellation.
- Default API origin and the `THROUGHLINE_API_KEY` environment variable are
  supported; existing explicit-origin constructors remain compatible.
- Reference Python client: cursor handling, upsert-as-replacement, withdrawal
  removal and reinstatement, idempotent replay, `Retry-After` on 429.
- `ThroughlinePublicationV3` example payload.
- Reference JavaScript client (`js/throughline.mjs`, ESM, no dependencies)
  with the same 22 test cases as the Python client.

The publication contract is not frozen. Breaking changes to it will be listed
here with the date they took effect.
