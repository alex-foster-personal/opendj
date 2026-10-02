import { ApiError } from '../api/client';
import type { components } from '../api-types';

/**
 * Operator-facing pin buckets for pin 6af63c5e9b7c / FB-20: the maintainer's vocabulary
 * on the comment-icon hover, not raw lifecycle field names.
 *
 * The buckets are computed by the daemon (GET /api/v1/feedback/comments/summary,
 * apps/webui/server/routes/feedback_summary.py), which is the only place that
 * can read the progress-tree ledger. This module only renders them, so there is
 * one bucket rule, not a server copy and a client copy that can drift.
 */
export type CommentSummary = components['schemas']['CommentSummaryOut'];
export type PinOperatorBreakdown = components['schemas']['PinOperatorBreakdownOut'];

/** Single-line title for the comment-pin control (pin 6af63c5e9b7c). */
export function describePinOperatorSummary(breakdown: PinOperatorBreakdown): string {
	const parts = [
		`${breakdown.sent_to_queue} sent to queue`,
		`${breakdown.in_progress} in-progress`,
		`${breakdown.delegated} delegated`,
		`${breakdown.fixed} fixed`,
		`${breakdown.merged} merged`
	];
	if (breakdown.blocked > 0) parts.push(`${breakdown.blocked} blocked`);
	if (breakdown.harvested > 0) parts.push(`${breakdown.harvested} harvested`);
	return `Active comment pins: ${breakdown.total} total - ${parts.join(', ')}`;
}

/**
 * Explainer line for a summary whose in-progress count could NOT include
 * fleet work (no progress ledger on this daemon, or one that does not parse).
 * Null when correlation ran, so nothing is said that is not true.
 */
export function describeFleetCorrelation(
	correlation: CommentSummary['fleet_correlation']
): string | null {
	if (correlation === 'ledger_missing') {
		return 'In-progress excludes fleet work: this daemon has no progress ledger to correlate issues against.';
	}
	if (correlation === 'ledger_unreadable') {
		return 'In-progress excludes fleet work: the progress ledger on this daemon did not parse.';
	}
	return null;
}

/** Statuses a retry can fix: the daemon was busy, restarting, or behind a
 * proxy that gave up. Anything else is the daemon (or its data) refusing. */
const TRANSIENT_SUMMARY_STATUSES = new Set([408, 429, 502, 503, 504]);

export type SummaryFailure =
	| { kind: 'missing' }
	| { kind: 'transient' }
	| { kind: 'error'; message: string };

/** How a failed GET /comments/summary must show (PR #4094 Sol P1):
 * - `missing`: 404, a daemon without the route; clear the summary, no error;
 * - `transient`: unreachable daemon (fetch rejects with a TypeError) or
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
