# Throughline reference clients

[Throughline](https://throughline.markets) is a free, rate-limited news API for
Polymarket: it tells you which news articles are relevant to which contracts,
and why. **It is not serving yet.** This client encodes the published contract
so integrations can be built and tested against it before the feed opens.
Spec: <https://throughline.markets/api>.

Two clients, same behaviour: `client.py` (Python 3.10+, stdlib only) and
`js/throughline.mjs` (any runtime with `fetch`, no dependencies, typed via
`js/throughline.d.ts`). Both enforce the change-stream semantics the contract
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

```js
import { Throughline, MatchStore } from "./js/throughline.mjs";

const tl = new Throughline(url, key);
const store = new MatchStore();
for await (const page of tl.changes(store.cursor)) store.apply(page);
```

Run the tests from the repository root: `python3 -I throughline/tests/test_client.py`
and `node --test throughline/js/tests/`. Each suite has the same 22 cases.

The contract is not frozen; breaking changes are listed in `CHANGELOG.md`.
