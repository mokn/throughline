import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Change, MatchStore, Page, RateLimited, Throughline, ThroughlineError, parsePage, retryAfter } from "../throughline.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const EXAMPLE = JSON.parse(readFileSync(join(here, "../../schema/upsert-single.json"), "utf8"));

const change = (cursor, { operation = "upsert", record = null, stream = "s", rid = "r" } = {}) =>
  new Change(cursor, stream, rid, operation, record, "ThroughlinePublicationV3");

// -- parsing -----------------------------------------------------------------

test("parses the published example", () => {
  const page = parsePage(EXAMPLE, 0);
  assert.equal(page.changes.length, 1);
  assert.equal(page.nextCursor, 1);
  assert.equal(page.hasMore, false);
  const c = page.changes[0];
  assert.equal(c.schema, "ThroughlinePublicationV3");
  assert.equal(c.operation, "upsert");
  assert.equal(c.key, `test_stream\u0000${EXAMPLE.items[0].envelope.record_id}`);
  assert.equal(c.isWithdrawal, false);
});

test("empty page keeps the caller moving", () => {
  const page = parsePage({ items: [], next_cursor: 97, has_more: false }, 12);
  assert.deepEqual(page.changes, []);
  assert.equal(page.nextCursor, 97);
});

test("missing next_cursor does not reset to zero", () => {
  assert.equal(parsePage({ items: [] }, 4096).nextCursor, 4096);
});

test("missing items key", () => {
  assert.deepEqual(parsePage({ next_cursor: 1 }, 0).changes, []);
});

test("null record is a withdrawal", () => {
  const page = parsePage({ items: [{ cursor: 9, envelope: { stream_id: "s", record_id: "r", operation: "withdraw", record: null } }], next_cursor: 9, has_more: false }, 0);
  assert.equal(page.changes[0].isWithdrawal, true);
});

test("unknown operation without record still removes", () => {
  assert.equal(change(1, { operation: "tombstone", record: null }).isWithdrawal, true);
});

test("unknown operation with record is not a withdrawal", () => {
  assert.equal(change(1, { operation: "replace", record: { a: 1 } }).isWithdrawal, false);
});

// -- store -------------------------------------------------------------------

const rec = () => ({ market: { condition_id: "0xabc" }, title: "first" });

test("upsert, withdrawal, reinstatement", () => {
  const s = new MatchStore();
  s.apply(new Page([change(1, { record: rec() })], 1, false));
  assert.equal(s.size, 1);
  s.apply(new Page([change(2, { operation: "withdraw", record: null })], 2, false));
  assert.equal(s.size, 0);
  s.apply(new Page([change(3, { record: rec() })], 3, false));
  assert.equal(s.size, 1);
});

test("upsert replaces rather than merges", () => {
  const s = new MatchStore();
  s.apply(new Page([change(1, { record: { a: 1, b: 2 } })], 1, false));
  s.apply(new Page([change(2, { record: { a: 9 } })], 2, false));
  assert.deepEqual(s.current(), [{ a: 9 }]);
});

test("replay is idempotent", () => {
  const s = new MatchStore();
  const page = new Page([change(1, { record: rec() })], 1, false);
  s.apply(page); s.apply(page); s.apply(page);
  assert.equal(s.size, 1);
  assert.equal(s.cursor, 1);
});

test("replaying a stale page cannot resurrect a withdrawal", () => {
  const s = new MatchStore();
  s.apply(new Page([change(1, { record: rec() })], 1, false));
  s.apply(new Page([change(2, { operation: "withdraw", record: null })], 2, false));
  s.apply(new Page([change(1, { record: rec() })], 1, false));
  assert.equal(s.size, 0);
});

test("cursor never goes backwards", () => {
  const s = new MatchStore();
  s.apply(new Page([], 50, false));
  s.apply(new Page([], 10, false));
  assert.equal(s.cursor, 50);
});

test("two streams sharing a record id are distinct matches", () => {
  const s = new MatchStore();
  s.apply(new Page([change(1, { record: { t: "a" }, stream: "x" })], 1, false));
  s.apply(new Page([change(2, { record: { t: "b" }, stream: "y" })], 2, false));
  assert.equal(s.size, 2);
});

test("forMarket filters and tolerates a missing market", () => {
  const s = new MatchStore();
  s.apply(new Page([change(1, { record: rec(), rid: "r1" })], 1, false));
  s.apply(new Page([change(2, { record: { title: "no market" }, rid: "r2" })], 2, false));
  assert.equal(s.forMarket("0xabc").length, 1);
  assert.deepEqual(s.forMarket("0xdef"), []);
});

// -- retry-after ------------------------------------------------------------

test("retryAfter values", () => {
  assert.equal(retryAfter("12"), 12);
  assert.equal(retryAfter("0"), 0);
  assert.equal(retryAfter(null), 5);
  assert.equal(retryAfter(undefined), 5);
  assert.equal(retryAfter(""), 5);
  assert.equal(retryAfter("soon"), 5);
  assert.equal(retryAfter("-3"), 0);
});

// -- transport ---------------------------------------------------------------

const ok = (body) => ({ ok: true, status: 200, headers: new Headers(), json: async () => body, text: async () => JSON.stringify(body) });
const fail = (status, body = "{}", headers = {}) => ({ ok: false, status, headers: new Headers(headers), json: async () => JSON.parse(body), text: async () => body });

test("requires baseUrl and apiKey and a fetch", () => {
  assert.throws(() => new Throughline("", "k"), TypeError);
  assert.throws(() => new Throughline("http://x", ""), TypeError);
  assert.throws(() => new Throughline("http://x", "k", { fetch: 42 }), TypeError);
});

test("sends bearer token and omits undefined params", async () => {
  const captured = {};
  const fetch = async (url, init) => { captured.url = url; captured.auth = init.headers.Authorization; return ok(EXAMPLE); };
  await new Throughline("https://api.example/", "tl_secret", { fetch }).page(0);
  assert.equal(captured.auth, "Bearer tl_secret");
  assert.match(captured.url, /cursor=0/);
  assert.doesNotMatch(captured.url, /market_id/);
  assert.doesNotMatch(captured.url, /limit/);
  assert.match(captured.url, /^https:\/\/api\.example\/v1\/news\?/);
});

test("429 honours Retry-After then succeeds", async () => {
  const slept = [];
  let n = 0;
  const fetch = async () => (++n === 1 ? fail(429, "slow down", { "Retry-After": "7" }) : ok(EXAMPLE));
  const page = await new Throughline("https://api.example", "k", { fetch, sleep: async (s) => { slept.push(s); } }).page(0);
  assert.deepEqual(slept, [7]);
  assert.equal(page.changes.length, 1);
});

test("429 gives up after maxRetries", async () => {
  const fetch = async () => fail(429, "nope", { "Retry-After": "1" });
  const client = new Throughline("https://api.example", "k", { fetch, maxRetries: 2, sleep: async () => {} });
  await assert.rejects(client.page(0), (e) => e instanceof RateLimited && e.retryAfter === 1 && e.status === 429);
});

test("client errors reject without retrying", async () => {
  for (const code of [400, 401, 404, 409, 413]) {
    let calls = 0;
    const fetch = async () => { calls += 1; return fail(code); };
    const client = new Throughline("https://api.example", "k", { fetch, sleep: async () => {} });
    await assert.rejects(client.page(0), (e) => e instanceof ThroughlineError && e.status === code && !(e instanceof RateLimited));
    assert.equal(calls, 1, `${code} must not be retried`);
  }
});

test("match returns null for a withdrawn head", async () => {
  const body = { envelope: { stream_id: "s", record_id: "r", operation: "withdraw", record: null } };
  const fetch = async () => ok(body);
  assert.equal(await new Throughline("https://api.example", "k", { fetch }).match("r", { streamId: "s" }), null);
});

test("changes walks to the head and advances on empty pages", async () => {
  const pages = [
    { items: [{ cursor: 1, envelope: { stream_id: "s", record_id: "a", operation: "upsert", record: {} } }], next_cursor: 1, has_more: true },
    { items: [], next_cursor: 2, has_more: true },
    { items: [{ cursor: 3, envelope: { stream_id: "s", record_id: "b", operation: "upsert", record: {} } }], next_cursor: 3, has_more: false },
  ];
  const seen = [];
  const fetch = async (url) => { seen.push(url); return ok(pages.shift()); };
  const collected = [];
  for await (const p of new Throughline("https://api.example", "k", { fetch }).changes(0)) collected.push(p);
  assert.equal(collected.length, 3);
  assert.equal(collected.reduce((n, p) => n + p.changes.length, 0), 2);
  assert.match(seen[1], /cursor=1/);
  assert.match(seen[2], /cursor=2/);
});

test("production origin defaults without changing existing explicit calls", async () => {
  let seen;
  const client = new Throughline(undefined, "test-key", { fetch: async (url) => { seen = url; return ok(EXAMPLE); } });
  await client.page();
  assert.match(seen, /^https:\/\/api\.throughline\.markets\/v1\/news\?/);
});

test("account quotas expose recovery immediately and never sleep", async () => {
  for (const code of ["account_quota_exceeded", "account_data_limit", "coverage_quota_exceeded", "management_quota_exceeded", "billing_quota_exceeded"]) {
    let calls = 0, slept = 0;
    const body = JSON.stringify({ error: code, message: "Allowance reached", resets_at: 86400, request_id: "request-1", upgrade_url: "https://api.throughline.markets/account#upgrade" });
    const client = new Throughline(undefined, "k", {
      fetch: async () => { calls++; return fail(429, body, { "Retry-After": "1" }); },
      sleep: async () => { slept++; },
    });
    await assert.rejects(client.page(), (e) => e instanceof RateLimited && e.code === code && e.resetsAt === 86400 && e.requestId === "request-1" && e.message.includes("Allowance reached"));
    assert.equal(calls, 1); assert.equal(slept, 0);
  }
});

test("long Retry-After fails immediately without retrying sooner", async () => {
  for (const status of [429, 503]) {
    let calls = 0, slept = 0;
    const client = new Throughline(undefined, "k", {
      fetch: async () => { calls++; return fail(status, '{"error":"burst_limited"}', { "Retry-After": "86400" }); },
      sleep: async () => { slept++; },
    });
    await assert.rejects(client.page(), ThroughlineError);
    assert.equal(calls, 1); assert.equal(slept, 0);
  }
});

test("retry waits share one total budget", async () => {
  let calls = 0;
  const slept = [];
  const client = new Throughline(undefined, "k", { retryBudget: 45,
    fetch: async () => { calls++; return fail(429, '{"error":"burst_limited"}', { "Retry-After": "20" }); },
    sleep: async (seconds) => slept.push(seconds),
  });
  await assert.rejects(client.page(), RateLimited);
  assert.deepEqual(slept, [20, 20]); assert.equal(calls, 3);
});

test("503 retries briefly and returns the page", async () => {
  let calls = 0;
  const slept = [];
  const client = new Throughline(undefined, "k", {
    fetch: async () => ++calls === 1 ? fail(503, '{"error":"service_busy"}', { "Retry-After": "1" }) : ok(EXAMPLE),
    sleep: async (seconds) => slept.push(seconds),
  });
  assert.equal((await client.page()).changes.length, 1);
  assert.deepEqual(slept, [1]); assert.equal(calls, 2);
});

test("abort cancels the built-in retry wait without another request", async () => {
  const controller = new AbortController();
  let calls = 0;
  const client = new Throughline(undefined, "k", { signal: controller.signal,
    fetch: async () => { calls++; queueMicrotask(() => controller.abort(new Error("cancelled"))); return fail(503, "busy", { "Retry-After": "10" }); },
  });
  await assert.rejects(client.page(), /cancelled/); assert.equal(calls, 1);
});

test("nonfinite Retry-After and malformed JSON error bodies are safe", () => {
  for (const value of ["NaN", "Infinity", "-Infinity"]) assert.equal(retryAfter(value), 5);
  for (const body of ["[]", "null", '{"error":[]}', "not json"]) assert.equal(new ThroughlineError(429, body).code, null);
});

test("zero retry budget permits the first call", async () => {
  const client = new Throughline(undefined, "k", { retryBudget: 0, fetch: async () => ok(EXAMPLE) });
  assert.equal((await client.page()).changes.length, 1);
});

test("HTTP-date Retry-After preserves the server delay", () => {
  const now = Date.now;
  try {
    Date.now = () => 0;
    assert.equal(retryAfter("Fri, 02 Jan 1970 00:00:00 GMT"), 86400);
  } finally { Date.now = now; }
});
