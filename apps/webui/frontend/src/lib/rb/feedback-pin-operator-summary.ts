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
