/**
 * The "Separate this track" button's words (STEM-77), kept out of
 * stem-status.ts on purpose: that module is in the initial load of "/"
 * (performance-ipc reads stemStatusView), and only a deck renders these.
 */

/**
 * What the "Separate this track" action on a NO STEMS deck says (STEM-77).
 *
 *   ✔︎ ✅ 🎯 A deck with no stems offers to make them, and says why when it cannot.
 *       [if] no stems source answers on this machine [then] the button is inert with that reason
 *       [if] the source is still being checked [then] the button is inert and says so
 *       [if] the start request is in flight [then] the button is inert (no second job on a double click)
 *       [if] a job is queued or running [then] the button names that job, inert
 *       [if] the only job is a queued library job the engine has not confirmed for this track [then] or one at another tier or executor [then] the button asks the engine, or shows the check or refusal first, never QUEUED (Sol on #6819)
 *       [if] the job ended but the deck has not looked for the bundle yet [then] the button is inert and says so
 *       [if] the deck's look for the bundle did not run (stem_load refused) [then] the button offers to look again, never to separate
 *       [if] the job succeeded but the deck looked and still has none [then] the button separates again (never a dead end, never a load loop)
 *       [if] the job failed, or did not start [then] the button offers to try again and carries the error
 *       [if] the library answered "never" [then] one track can still be separated (no input reads it)
 */
export interface SeparateActionView {
	label: string;
	tip: string;
	disabled: boolean;
}

export interface SeparateActionInput {
	/** Why separation cannot start here (jobs API, local executor or GPU transport), or null. */
	refusal: string | null;
	/** The source check has not answered yet. */
	checking: boolean;
	/** The newest separation job covering this track: its own (`track`) or a
	 * library-wide `pending` scope job that includes it. */
	job: { id: string; status: string; error?: string | null; scope?: 'track' | 'pending'; confirmed?: boolean } | null;
	/** Why the last attempt to start a job was refused, or null. */
	enqueueError: string | null;
	/** The enqueue request is in flight. */
	starting: boolean;
	/** Since `job` ended, the deck has looked for the track's bundle and found
	 * none (stem-separate-probe.ts). Until then an ended job's status says
	 * nothing about this track: a library job can fail after writing it. */
	probed: boolean;
	/** The deck's last look for the bundle after `job` ended did not run
	 * (stem_load refused or failed), so nothing is known about this track. */
	probeFailed?: boolean;
}

export function separateActionView(input: SeparateActionInput): SeparateActionView {
	const { refusal, checking, job, enqueueError, starting, probed, probeFailed = false } = input;
	if (starting) {
		return { label: 'STARTING', tip: 'Starting a separation job for this track...', disabled: true };
	}
	if (job !== null && job.confirmed === false && (job.status === 'queued' || job.status === 'running' || job.status === 'cancelling')) {
		// Not known to cover this track, so it is never shown as doing so:
		// the check in flight or the refusal comes first (Sol on #6819).
		if (checking) {
			return { label: 'SEPARATE', tip: 'Checking whether this machine can separate stems...', disabled: true };
		}
		if (refusal !== null) {
			return { label: 'SEPARATE', tip: `Separation is not available here: ${refusal}.`, disabled: true };
		}
		return {
			label: 'SEPARATE',
			tip:
				job.scope === 'pending' && job.status === 'queued'
					? `A library-wide separation (job ${job.id}) is queued and may include this track. Click to ask the engine: it hands that job back if it does, and separates this track if not.`
					: `A separation (job ${job.id}, ${job.status}) runs at another tier or on another executor than this deck would ask for. Click to ask the engine: it hands back a job that makes these stems, and separates this track if none does.`,
			disabled: false
		};
	}
	if (job !== null && (job.status === 'queued' || job.status === 'running' || job.status === 'cancelling')) {
		return {
			label: job.status === 'queued' ? 'QUEUED' : 'SEPARATING',
			tip:
				job.scope === 'pending'
					? `Separating the library (job ${job.id}, ${job.status}), which includes this track. The stem buttons switch on when it finishes.`
					: `Separating this track (job ${job.id}, ${job.status}). The stem buttons switch on when it finishes.`,
			disabled: true
		};
	}
	if (job !== null && !probed && probeFailed) {
		return {
			label: 'CHECK STEMS',
			tip: `Separation job ${job.id} ended (${job.status}), but looking for this track's stems did not run. Click to look again.`,
			disabled: false
		};
	}
	if (job !== null && !probed) {
		return {
			label: 'CHECKING STEMS',
			tip: `Separation job ${job.id} ended (${job.status}); looking for this track's stems before saying more.`,
			disabled: true
		};
	}
	if (checking) {
		return { label: 'SEPARATE', tip: 'Checking whether this machine can separate stems...', disabled: true };
	}
	if (refusal !== null) {
		return { label: 'SEPARATE', tip: `Separation is not available here: ${refusal}.`, disabled: true };
	}
	if (enqueueError !== null) {
		return { label: 'SEPARATE', tip: `Separation did not start: ${enqueueError}. Click to try again.`, disabled: false };
	}
	if (job !== null && job.status === 'succeeded') {
		// The deck already looked for the bundle after this job (probed) and
		// found none, so loading again would find none again: separate.
		return {
			label: 'SEPARATE AGAIN',
			tip: `Separation finished (job ${job.id}) but no stems were found for this track. Click to separate it again.`,
			disabled: false
		};
	}
	if (job !== null) {
		return {
			label: 'SEPARATE AGAIN',
			tip: `Separation ${job.status} (job ${job.id}): ${job.error ?? 'no reason given'}. Click to try again.`,
			disabled: false
		};
	}
	return { label: 'SEPARATE', tip: 'Separate this track into stems. Works even if the library is set to never separate.', disabled: false };
}
