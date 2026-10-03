import { ApiError } from '../api/client';
import type { CommentSummary } from './feedback-pin-operator-summary';

/*
 * Failure classification and body shape check for GET
 * /api/v1/feedback/comments/summary (FB-20). Kept apart from the hover copy in
 * feedback-pin-operator-summary.ts so the store can import this with a dynamic
 * import on the first summary refresh instead of shipping it with "/" and
 * /performance (PR #4094: both routes' bundle budgets).
 */

/** Statuses a retry can fix: the daemon was busy, restarting, or behind a
 * proxy that gave up. Anything else is the daemon (or its data) refusing. */
const TRANSIENT_SUMMARY_STATUSES = new Set([408, 429, 502, 503, 504]);

export type SummaryFailure =
	| { kind: 'missing' }
	| { kind: 'transient' }
	| { kind: 'error'; message: string };

/** How a failed GET /comments/summary must show (PR #4094 Sol P1):
 * - `missing`: 404, a daemon without the route; clear the summary, no error;
 * - `transient`: unreachable daemon (the request rejects with a TypeError; the
 *   store reads the body separately through `decodeSummaryBody`, so a body
 *   decode failure never arrives here as a TypeError) or
 *   408/429/502/503/504; keep the last-known counts and retry on the next poll;
 * - `error`: anything else (a 500 such as unknown_pin_status from a malformed
 *   persisted pin, another 4xx, an empty or undecodable body) is persistent, so
 *   the counts are dropped and the reason is shown (fail fast, no silent
 *   fallback to stale numbers). */
export function classifySummaryFailure(err: unknown): SummaryFailure {
	if (err instanceof ApiError) {
		if (err.status === 404) return { kind: 'missing' };
		if (TRANSIENT_SUMMARY_STATUSES.has(err.status)) return { kind: 'transient' };
		return { kind: 'error', message: `HTTP ${err.status} ${err.code}: ${err.message}` };
	}
	if (err instanceof TypeError) return { kind: 'transient' };
	return { kind: 'error', message: err instanceof Error ? err.message : String(err) };
}

const OPERATOR_KEYS = [
	'total',
	'sent_to_queue',
	'in_progress',
	'delegated',
	'fixed',
	'merged',
	'blocked',
	'harvested'
] as const;
const LIFECYCLE_KEYS = [
	'total',
	'untriaged',
	'open',
	'issued',
	'blocked',
	'fixed',
	'merged',
	'harvested'
] as const;
const FLEET_CORRELATIONS = new Set(['ok', 'ledger_missing', 'ledger_unreadable']);

function bucketProblem(body: Record<string, unknown>, name: string, keys: readonly string[]) {
	const buckets = body[name];
	if (typeof buckets !== 'object' || buckets === null) return `${name} is missing`;
	for (const key of keys) {
		const n = (buckets as Record<string, unknown>)[key];
		if (!Number.isInteger(n) || (n as number) < 0) return `${name}.${key} is ${JSON.stringify(n)}`;
	}
	return null;
}

/** Check a 2xx GET /comments/summary body against CommentSummaryOut before it
 * is stored (PR #4094 Sol P1): valid JSON of the wrong shape (`{}`, a missing
 * or non-integer bucket, an unknown fleet_correlation) would otherwise render
 * as undefined counts or throw inside the controls. Throws a plain Error, which
 * `classifySummaryFailure` treats as persistent, so the controls show it. */
export function parseCommentSummary(body: unknown): CommentSummary {
	const fail = (why: string): never => {
		throw new Error(`malformed /comments/summary body: ${why}`);
	};
	if (typeof body !== 'object' || body === null || Array.isArray(body)) fail('not an object');
	const record = body as Record<string, unknown>;
	const problem =
		bucketProblem(record, 'operator', OPERATOR_KEYS) ??
		bucketProblem(record, 'lifecycle', LIFECYCLE_KEYS);
	if (problem) fail(problem);
	if (!FLEET_CORRELATIONS.has(record.fleet_correlation as string)) {
		fail(`fleet_correlation is ${JSON.stringify(record.fleet_correlation)}`);
	}
	return body as CommentSummary;
}

/** Decode a 2xx GET /comments/summary body read as a stream (PR #4094 Sol P1).
 * `Response.json()` rejects with a TypeError for a body that cannot be decoded
 * (for example a plain body labeled `Content-Encoding: gzip`), the same class a
 * failed request rejects with. Reading the body here, apart from the request,
 * turns every read or parse failure into a plain Error, which
 * `classifySummaryFailure` treats as persistent rather than transient. */
export async function decodeSummaryBody(stream: ReadableStream<Uint8Array>): Promise<unknown> {
	try {
		return JSON.parse(await new Response(stream).text());
	} catch (err) {
		throw new Error(
			`undecodable /comments/summary body: ${err instanceof Error ? err.message : String(err)}`
		);
	}
}
