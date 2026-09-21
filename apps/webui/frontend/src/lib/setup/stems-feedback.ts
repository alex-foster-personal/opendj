/**
 * What first-run setup says about the stems job it enqueued, read from the
 * LIVE job row rather than from the fact that the enqueue was accepted.
 *
 * Those are different facts, and the gap between them is exactly what a
 * tester hit on the test Mac (Wed 16 Sep 2026): the enqueue returned a job id,
 * the wizard moved on, and the job failed 198 ms later on a worker script the
 * installed app does not ship. Nothing on screen ever said so.
 *
 * Kept out of the Svelte component so every status is testable without a DOM.
 * The component owns WHICH job row to read (the jobs store); this owns what
 * that row means.
 */

import {
	JOB_STATUSES,
	errorTail,
	progressPct,
	type Job,
	type JobStatus
} from '$lib/rb/jobs-store.svelte';

export type StemsFeedbackState =
	| 'none'
	| 'pending'
	| 'queued'
	| 'running'
	| 'done'
	| 'failed'
	| 'cancelled'
	| 'unknown';

export type StemsFeedback = {
	state: StemsFeedbackState;
	/** Plain-language line. Carries the engine's own error text in the failed
	 *  state, trimmed to its tail but never paraphrased. */
	message: string;
};

type JobRow = Pick<Job, 'status' | 'progress' | 'error'>;

function exhaustive(status: never): never {
	throw new Error(`Unhandled job status: ${String(status)}`);
}

/**
 * @param jobId the id the enqueue returned, or null when nothing was started
 * @param job the store's row for that id, or null when no update has arrived
 */
export function stemsJobFeedback(jobId: string | null, job: JobRow | null): StemsFeedback {
	if (jobId === null) return { state: 'none', message: '' };
	// An id with no row yet is the normal first instant after an enqueue. It is
	// not evidence of anything, so it must not read as a failure.
	if (job === null) {
		return { state: 'pending', message: 'Stem separation was requested; waiting for the engine to pick it up.' };
	}
	// The wire type is a plain string. A value outside the engine's status list
	// is contract drift: report it as unknown rather than guess what it meant.
	if (!(JOB_STATUSES as readonly string[]).includes(job.status)) {
		return {
			state: 'unknown',
			message: `The engine reported a job status this screen does not know (${job.status}), so whether stem separation ran is unknown.`
		};
	}
	const status = job.status as JobStatus;
	switch (status) {
		case 'queued':
			return { state: 'queued', message: 'Stem separation is queued and will start shortly.' };
		case 'running':
			return {
				state: 'running',
				message: `Stem separation is running in the background, ${progressPct(job.progress)}% done.`
			};
		case 'succeeded':
			return { state: 'done', message: 'Stem separation finished.' };
		case 'failed':
			return {
				state: 'failed',
				message:
					'Stem separation did not run' +
					(job.error ? `: ${errorTail(job.error)}` : ', and the engine gave no reason.')
			};
		case 'cancelling':
			return { state: 'cancelled', message: 'Stem separation is being cancelled.' };
		case 'cancelled':
			return { state: 'cancelled', message: 'Stem separation was cancelled.' };
		case 'unknown':
			return {
				state: 'unknown',
				message: 'The engine lost track of the stem separation job, so whether it ran is unknown.'
			};
		default:
			return exhaustive(status);
	}
}
