export declare class ThroughlineError extends Error {
  status: number;
  body: string;
  code: string | null;
  details: Record<string, unknown>;
  requestId?: string;
  upgradeUrl?: string;
  resetsAt?: number;
  constructor(status: number, body: string);
}
export declare class RateLimited extends ThroughlineError {
  retryAfter: number;
  constructor(status: number, body: string, retryAfter: number);
}
export declare class Change {
  readonly cursor: number;
  readonly streamId: string;
  readonly recordId: string;
  readonly operation: string;
  readonly record: Record<string, unknown> | null;
  readonly schema: string;
  constructor(cursor: number, streamId: string, recordId: string, operation: string, record: Record<string, unknown> | null, schema: string);
  get key(): string;
  get isWithdrawal(): boolean;
}
export declare class Page {
  readonly changes: Change[];
  readonly nextCursor: number;
  readonly hasMore: boolean;
  constructor(changes: Change[], nextCursor: number, hasMore: boolean);
}
export declare function retryAfter(value: string | null | undefined, fallback?: number): number;
export declare function parsePage(payload: unknown, fallbackCursor: number): Page;
export interface ThroughlineOptions {
  timeout?: number;
  maxRetries?: number;
  retryBudget?: number;
  signal?: AbortSignal;
  sleep?: (seconds: number, signal?: AbortSignal) => Promise<void>;
  fetch?: typeof fetch;
}
export interface PageOptions {
  limit?: number;
  marketId?: string;
}
export declare class Throughline {
  constructor(baseUrl?: string, apiKey?: string, opts?: ThroughlineOptions);
  page(cursor?: number, opts?: PageOptions): Promise<Page>;
  match(recordId: string, opts?: { streamId?: string }): Promise<Record<string, unknown> | null>;
  coverage(): Promise<Record<string, unknown>>;
  changes(cursor?: number, opts?: PageOptions): AsyncGenerator<Page, void, undefined>;
}
export declare const DEFAULT_BASE_URL: string;
export declare class MatchStore {
  get cursor(): number;
  get size(): number;
  apply(page: Page): void;
  current(): Record<string, unknown>[];
  forMarket(conditionId: string): Record<string, unknown>[];
}
