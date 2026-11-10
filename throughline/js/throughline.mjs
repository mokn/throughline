/**
 * Reference client for the Throughline news relevance feed.
 *
 * No dependencies, on purpose: an MCP server and a cron job should not inherit
 * a dependency tree to read a feed. Needs a runtime with global `fetch`
 * (Node 18+, Deno, Bun, browsers).
 *
 * The feed is a change stream, not a list. Three things are easy to get wrong
 * and all three are handled here, because getting them wrong leaves wrong data
 * in your copy forever:
 *
 * 1. An `upsert` is a COMPLETE REPLACEMENT of that record, not a patch.
 * 2. A withdrawal arrives as `record: null`. It is never inferred from absence,
 *    and a later upsert may reinstate the same match.
 * 3. The cursor must be persisted TOGETHER WITH the records it brought, and
 *    never advanced before they are saved. Otherwise a crash between the two
 *    skips a change permanently.
 *
 * A match is identified by the `(stream_id, record_id)` pair, not by
 * `record_id` alone.
 */

const UA = "throughline-js/0.1 (+https://throughline.markets/api)";
export const DEFAULT_BASE_URL = "https://api.throughline.markets";
const TRANSIENT = new Set(["burst_limited", "burst_capacity", "server_capacity"]);

export class ThroughlineError extends Error {
  /** @param {number} status @param {string} body */
  constructor(status, body) {
    let details;
    try { details = JSON.parse(body); } catch { details = {}; }
    if (!details || typeof details !== "object" || Array.isArray(details)) details = {};
    super(`HTTP ${status}: ${details.message || details.error || body.slice(0, 400)}`);
    this.name = "ThroughlineError";
    this.status = status;
    this.body = body;
    this.details = details;
    this.code = typeof details.error === "string" ? details.error : null;
    this.requestId = details.request_id;
    this.upgradeUrl = details.upgrade_url;
    this.resetsAt = details.resets_at;
  }
}

export class RateLimited extends ThroughlineError {
  /** @param {number} status @param {string} body @param {number} retryAfter seconds */
  constructor(status, body, retryAfter) {
    super(status, body);
    this.name = "RateLimited";
    this.retryAfter = retryAfter;
  }
}

/** One change from the stream. */
export class Change {
  /**
   * @param {number} cursor
   * @param {string} streamId
   * @param {string} recordId
   * @param {string} operation
   * @param {object|null} record
   * @param {string} schema
   */
  constructor(cursor, streamId, recordId, operation, record, schema) {
    this.cursor = cursor;
    this.streamId = streamId;
    this.recordId = recordId;
    this.operation = operation;
    this.record = record;
    this.schema = schema;
    Object.freeze(this);
  }

  /** The identity of the match this change applies to. */
  get key() {
    return `${this.streamId}\u0000${this.recordId}`;
  }

  /**
   * True when this change removes the match from current views. Driven by
   * `record === null` rather than by the operation name, so an unrecognised
   * operation that carries no record is still treated as a removal rather
   * than silently ignored.
   */
  get isWithdrawal() {
    return this.record === null || this.record === undefined;
  }
}

export class Page {
  /** @param {Change[]} changes @param {number} nextCursor @param {boolean} hasMore */
  constructor(changes, nextCursor, hasMore) {
    this.changes = changes;
    this.nextCursor = nextCursor;
    this.hasMore = hasMore;
    Object.freeze(this);
  }
}

/** @param {string|null|undefined} value @param {number} [fallback] */
export function retryAfter(value, fallback = 5) {
  if (value === null || value === undefined || value === "") return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) {
    const date = /^[A-Za-z]{3}, .+ GMT$/.test(value) ? Date.parse(value) : NaN;
    return Number.isFinite(date) ? Math.max(0, (date - Date.now()) / 1000) : fallback;
  }
  return Math.max(0, n);
}

/** @param {any} payload @param {number} fallbackCursor */
export function parsePage(payload, fallbackCursor) {
  const changes = [];
  for (const item of payload?.items ?? []) {
    const env = item?.envelope ?? {};
    changes.push(new Change(
      Number(item?.cursor ?? 0),
      String(env.stream_id ?? ""),
      String(env.record_id ?? ""),
      String(env.operation ?? ""),
      env.record === undefined ? null : env.record,
      String(env.schema ?? ""),
    ));
  }
  const next = payload?.next_cursor;
  return new Page(
    changes,
    // A missing next_cursor must not silently reset the caller to zero.
    next === null || next === undefined ? fallbackCursor : Number(next),
    Boolean(payload?.has_more ?? false),
  );
}

const sleepMs = (s, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(signal.reason);
  const abort = () => { clearTimeout(timer); reject(signal.reason); };
  const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, s * 1000);
  signal?.addEventListener("abort", abort, { once: true });
});

/**
 * A read-only client. Defaults to the production origin. In Node the key may
 * come from THROUGHLINE_API_KEY; browsers must supply one explicitly.
 * Account quotas fail immediately; transient reads retry within retryBudget.
 */
export class Throughline {
  /**
   * @param {string} baseUrl
   * @param {string} apiKey
   * @param {{timeout?: number, maxRetries?: number, retryBudget?: number, signal?: AbortSignal, sleep?: (seconds: number, signal?: AbortSignal) => Promise<void>, fetch?: typeof fetch}} [opts]
   */
  constructor(baseUrl = DEFAULT_BASE_URL, apiKey = globalThis.process?.env?.THROUGHLINE_API_KEY, opts = {}) {
    if (!baseUrl) throw new TypeError("baseUrl is required");
    if (!apiKey) throw new TypeError("apiKey is required");
    this._base = baseUrl.replace(/\/+$/, "");
    this._key = apiKey;
    this._timeout = opts.timeout ?? 30;
    this._maxRetries = opts.maxRetries ?? 5;
    this._retryBudget = opts.retryBudget ?? 60;
    if (!Number.isFinite(this._timeout) || this._timeout <= 0 ||
        !Number.isFinite(this._retryBudget) || this._retryBudget < 0 ||
        !Number.isInteger(this._maxRetries) || this._maxRetries < 0) throw new TypeError("invalid timeout or retry policy");
    this._signal = opts.signal;
    this._sleep = opts.sleep ?? sleepMs;
    this._fetch = opts.fetch ?? globalThis.fetch;
    if (typeof this._fetch !== "function") throw new TypeError("a fetch implementation is required");
  }

  async _get(path, params = {}) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) q.set(k, String(v));
    const qs = q.toString();
    const url = `${this._base}${path}${qs ? `?${qs}` : ""}`;
    const headers = { Authorization: `Bearer ${this._key}`, Accept: "application/json", "User-Agent": UA };
    let attempt = 0;
    let waited = 0;
    let lastError;
    const started = performance.now();
    for (;;) {
      this._signal?.throwIfAborted();
      let remaining = this._retryBudget - Math.max((performance.now() - started) / 1000, waited);
      if (attempt && remaining <= 0) throw lastError;
      const controller = new AbortController();
      const abort = () => controller.abort(this._signal.reason);
      this._signal?.addEventListener("abort", abort, { once: true });
      const timeout = attempt ? Math.min(this._timeout, remaining) : this._timeout;
      const timer = setTimeout(() => controller.abort(new DOMException("Request timed out", "TimeoutError")), timeout * 1000);
      let res, body;
      try {
        res = await this._fetch(url, { headers, signal: controller.signal, redirect: "error" });
        if (res.ok) return await res.json();
        body = await res.text();
      } finally {
        clearTimeout(timer);
        this._signal?.removeEventListener("abort", abort);
      }
      const wait = retryAfter(res.headers.get("Retry-After"), res.status === 503 ? 1 : 5);
      const error = res.status === 429 ? new RateLimited(res.status, body, wait) : new ThroughlineError(res.status, body);
      const transient = res.status === 503 || res.status === 429 && (!error.code || TRANSIENT.has(error.code));
      remaining = this._retryBudget - Math.max((performance.now() - started) / 1000, waited);
      if (transient && attempt < this._maxRetries && wait < remaining) {
        // Never shorten Retry-After to fit the retry budget.
        await this._sleep(wait, this._signal);
        waited += wait;
        attempt += 1;
        lastError = error;
        continue;
      }
      throw error;
    }
  }

  /**
   * One page of changes at or after `cursor`, oldest first. `cursor = 0` is
   * the start of retained history; there is no snapshot endpoint. `marketId`
   * is an exact condition ID, not a slug, event ID or token ID.
   * @param {number} [cursor] @param {{limit?: number, marketId?: string}} [opts]
   * @returns {Promise<Page>}
   */
  async page(cursor = 0, opts = {}) {
    const payload = await this._get("/v1/news", { cursor, limit: opts.limit, market_id: opts.marketId });
    return parsePage(payload, cursor);
  }

  /** Capture the global head once, persist the number, then poll numerically.
   * This empty poll counts as one request. Repeating it skips new publications.
   * @returns {Promise<number>}
   */
  async startFromNow() {
    const payload = await this._get("/v1/news", { cursor: "latest", limit: 1 });
    const cursor = payload?.next_cursor;
    if (!Number.isSafeInteger(cursor) || cursor < 0 || !Array.isArray(payload.items) ||
        payload.items.length !== 0 || payload.has_more !== false) {
      throw new ThroughlineError(502, '{"error":"invalid_start_cursor_response"}');
    }
    return cursor;
  }

  /**
   * The current head of one match, or `null` if it is withdrawn. Pass
   * `streamId` to disambiguate; without it a shared record ID returns 409.
   * @param {string} recordId @param {{streamId?: string}} [opts]
   */
  async match(recordId, opts = {}) {
    const payload = await this._get(`/v1/news/${encodeURIComponent(recordId)}`, { stream_id: opts.streamId });
    const env = payload?.envelope ?? payload;
    return env?.record ?? null;
  }

  /**
   * Supplied coverage and status facts, with unknowns left explicit. Read this
   * as well as the feed: a quiet feed and a broken pipeline look identical from
   * `/v1/news` alone, and a market's last match is not its last screen.
   */
  coverage() {
    return this._get("/v1/coverage");
  }

  /**
   * Yield pages until the head is reached. Whole pages, not changes, so the
   * caller can save records and cursor together in one transaction.
   * @param {number} [cursor] @param {{limit?: number, marketId?: string}} [opts]
   * @returns {AsyncGenerator<Page>}
   */
  async *changes(cursor = 0, opts = {}) {
    for (;;) {
      const page = await this.page(cursor, opts);
      yield page;
      cursor = page.nextCursor; // even an empty filtered page can advance the cursor
      if (!page.hasMore) return;
    }
  }
}

/**
 * An in-memory current view, applying the stream correctly. Swap this for
 * your own persistence; the point is the `apply` semantics: replacement
 * rather than merge, removal on withdrawal, reinstatement on a later upsert,
 * and idempotence on replay.
 */
export class MatchStore {
  constructor() {
    /** @type {Map<string, object>} */
    this._current = new Map();
    this._cursor = 0;
  }

  get cursor() {
    return this._cursor;
  }

  get size() {
    return this._current.size;
  }

  /** Apply a page, then adopt its cursor. Safe to replay. @param {Page} page */
  apply(page) {
    for (const c of page.changes) {
      if (c.cursor <= this._cursor) continue; // already applied; replay is free
      if (c.isWithdrawal) this._current.delete(c.key);
      else this._current.set(c.key, c.record); // complete replacement, never a merge
    }
    this._cursor = Math.max(this._cursor, page.nextCursor);
  }

  current() {
    return [...this._current.values()];
  }

  /** @param {string} conditionId */
  forMarket(conditionId) {
    return this.current().filter((r) => r?.market?.condition_id === conditionId);
  }
}
