# Changelog

## Unreleased

- Added the `throughline` command (version 0.2.0): delegated setup where a
  signed-in human approves a locally generated key, private-file storage with
  git-ignore, symlink, owner and mode checks, resumable pending state, account,
  key and billing reads under optional scopes, and a once-only start-from-now
  cursor. Requires the delegated-access server release; not a claim that those
  routes are deployed.

- Require HTTPS base URLs except explicit loopback development; reject credentials, queries and fragments in the base URL.

- Reject malformed pages, cursors and absent upsert records before they can corrupt saved state. Return deterministic response_too_large without retrying.

- Reject whitespace, control characters and non-ASCII credentials before transport without echoing the key in an exception.
- Start-from-now helpers require the foundation API release; publish clients only after that server release.

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
