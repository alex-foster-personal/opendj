/**
 * What the wizard's last screen says about analysis, read from the LIVE queue.
 *
 * A folder import has no rekordbox database behind it, so there is no ANLZ to
 * read a BPM, key or beatgrid out of. Own analysis is not a preference on that
 * path, it is the only source there is, and the daemon's analyze-on-import
 * loop is what supplies it (`GET /api/v1/analysis-queue`).
 *
 * This module is the READING of that queue, kept out of the Svelte component
 * so the four states it can be in are testable without a DOM. The component
 * owns the polling and the drain request; this owns what the numbers mean.
 *
 * The states are deliberately not ordered by how encouraging they are:
 * `failed` outranks `working` even though a failed drain leaves every track
 * pending, because "still going" is what a build that cannot analyze at all
 * would otherwise say forever.
 */

import type { AnalysisQueue } from '$lib/rb/api-ingest';

export type AnalysisFollowupState = 'unknown' | 'not-needed' | 'working' | 'done' | 'failed';

export type AnalysisFollowup = {
	state: AnalysisFollowupState;
	/** Tracks with no vendor analysis to fall back on: the real denominator. */
	total: number;
	analyzed: number;
	pending: number;
	/** Rows whose audio is not on this machine. Never folded into `analyzed`. */
	unreachable: number;
	/** Plain-language line for the panel. Carries the engine's own error text
	 *  verbatim in the failed state rather than a paraphrase of it. */
	message: string;
	/** True only when there is work AND no job is holding the one refresh slot.
	 *  False after a failure, so the screen cannot relaunch a drain that has
	 *  already said it cannot run. */
	shouldStartDrain: boolean;
};

const NOTHING: AnalysisFollowup = {
	state: 'unknown',
	total: 0,
	analyzed: 0,
	pending: 0,
	unreachable: 0,
	message: 'Checking what still needs analyzing...',
	shouldStartDrain: false
};

function unreachableClause(unreachable: number): string {
	if (unreachable === 0) return '';
	return ` ${unreachable} could not be analyzed because their audio is not on this Mac.`;
}

export function analysisFollowup(queue: AnalysisQueue | null): AnalysisFollowup {
	if (queue === null) return NOTHING;

	const base = {
		total: queue.unmapped,
		analyzed: queue.analyzed,
		pending: queue.pending,
		unreachable: queue.unreachable
	};

	// FAILED FIRST. A drain that raised leaves the queue exactly as full as one
	// that never started, so any check on `pending` alone reports "working".
	if (queue.job.error !== null) {
		return {
			...base,
			state: 'failed',
			message:
				'Open DJ could not work out the BPM, key and beatgrid for these ' +
				`tracks, so they have none: ${queue.job.error}`,
			shouldStartDrain: false
		};
	}
	if (queue.unmapped === 0) {
		return {
			...base,
			state: 'not-needed',
			message: 'Nothing in this library needs analyzing.',
			shouldStartDrain: false
		};
	}
	if (queue.pending > 0) {
		return {
			...base,
			state: 'working',
			message:
				'Open DJ is working out the BPM, key and beatgrid for these tracks ' +
				`itself, because a folder has no rekordbox analysis to read. ` +
				`${queue.analyzed} of ${queue.unmapped} done so far. You can start ` +
				'playing now; each track gets its grid as it finishes.',
			shouldStartDrain: !queue.job.running
		};
	}
	return {
		...base,
		state: 'done',
		message:
			`Open DJ worked out the BPM, key and beatgrid for ${queue.analyzed} of ` +
			`these tracks itself.${unreachableClause(queue.unreachable)}`,
		shouldStartDrain: false
	};
}
