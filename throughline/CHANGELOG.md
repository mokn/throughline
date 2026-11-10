# Changelog

## Unreleased

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
