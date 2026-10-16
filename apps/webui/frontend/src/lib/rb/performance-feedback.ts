/**
 * Performance feedback marks (PREF-02): the TopBar thumbs record a detached
 * snapshot through the typed `feedback_mark` command, and this module owns the
 * engine-side store write plus the cached summary that IPC query reads serve.
 *
 * Split out of the old vibe.svelte.ts when the vibe meter was removed (R10,
 * Wed 7 Oct 2026). Its localStorage history key `mdt.rb.vibe-history.v1` is
 * now unread and deliberately left in place: clearing it would need a boot
 * migration for a few KB of dead data.
 */

import {
	createPerformanceFeedback,
	getPerformanceFeedback,
	type PerformanceFeedbackMark,
	type PerformanceFeedbackSummary
} from '$lib/api';

export type { PerformanceFeedbackMark } from '$lib/api';

let _feedbackSummary: PerformanceFeedbackSummary = { count: 0, last_mark: null };
let _feedbackVersion = 0;
let _feedbackWrite: Promise<void> = Promise.resolve();

/** Hydrates the cached summary once from the engine-owned feedback store. */
export async function hydratePerformanceFeedback(): Promise<void> {
	const version = _feedbackVersion;
	const data = await getPerformanceFeedback();
	if (version === _feedbackVersion) _feedbackSummary = structuredClone(data);
}

/** Stores a detached dispatcher snapshot. It never reads the live audio model. */
export async function recordPerformanceFeedback(
	mark: PerformanceFeedbackMark
): Promise<PerformanceFeedbackSummary> {
	const detached = structuredClone(mark);
	const write = _feedbackWrite.catch(() => undefined).then(async () => {
		const data = await createPerformanceFeedback(detached);
		_feedbackVersion += 1;
		_feedbackSummary = structuredClone(data);
		return performanceFeedbackSummary();
	});
	_feedbackWrite = write.then(
		() => undefined,
		() => undefined
	);
	return write;
}

/** Cached summary for regular IPC query reads; history is never reparsed here. */
export function performanceFeedbackSummary(): PerformanceFeedbackSummary {
	return structuredClone(_feedbackSummary);
}
