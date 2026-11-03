# Throughline reference client

[Throughline](https://throughline.markets) is a free, rate-limited news API for
Polymarket: it tells you which news articles are relevant to which contracts,
and why. **It is not serving yet.** This client encodes the published contract
so integrations can be built and tested against it before the feed opens.
Spec: <https://throughline.markets/api>.

Stdlib only. The client enforces the change-stream semantics the contract
requires: an upsert replaces the record, a withdrawal is the record with
`null` content and stays readable, identity is `(stream_id, record_id)`, the
cursor never resets or reverses, replay is idempotent, `Retry-After` is
honoured on 429, and 4xx is never retried.

```python
from throughline import Throughline, MatchStore

tl = Throughline(url, key)
store = MatchStore()
for page in tl.changes(cursor=store.cursor):
    store.apply(page)
```

Run the tests: `python3 -I tests/test_client.py` from this directory's parent.

The contract is not frozen; breaking changes are listed in `CHANGELOG.md`.
