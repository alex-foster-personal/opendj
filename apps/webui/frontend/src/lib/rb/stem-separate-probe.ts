/**
 * After a separation job covering a NO STEMS deck's track ends, look for the
 * track's bundle before saying anything about the job (STEM-77).
 *
 * A library-wide `scope: pending` job, or a multi-track one, can write this
 * track's bundle and still end `failed` because ANOTHER track failed; a job
 * can also be cancelled after this track finished, or its status lost
 * (`unknown`). The job's status says nothing about this one track, so the
 * deck asks the engine instead: the `stem_load` command re-runs the stem
 * probe (retryDeckStems: a NO STEMS deck reloads, since the answer may have
 * changed). A bundle that exists loads; one that does not leaves the deck on
 * NO STEMS, and only then does the button carry the job's failure.
 *
 *   ✔︎ ✅ 🎯 A track whose bundle landed loads it, however its job ended.
 *       [if] the covering job failed, was cancelled or is unknown [then] the deck still probes
 *       [if] the job is still queued, running or cancelling [then] nothing is probed yet
 *       [if] the same ended job is seen again for the same track [then] it is not probed twice
 *       [if] the deck loads another track the same library job covered [then] that track is probed too
 *       [if] the probe has not answered [then] the job's failure is not shown
 *       [if] the stem_load did not run (refused, e.g. a preset lock, or threw) [then] the probe has NOT answered:
 *            it is marked failed, never retried on its own (each try toasts), and `retry` runs it again
 *       [if] the probe threw rather than refused [then] its error is kept (`error`) and rethrown, never read as a refusal
 *       [if] the stem_load returned while the stem load it started is still running [then] the look waits for that load
 *            to settle before it answers (lookForBundle; Codex on #6819)
 */

/** Statuses a separation job never leaves: the moment to look for the bundle. */
export const SEPARATE_ENDED_STATUSES: readonly string[] = ['succeeded', 'failed', 'cancelled', 'unknown'];

export function separateJobEnded(status: string): boolean {
	return SEPARATE_ENDED_STATUSES.includes(status);
}

/**
 * One deck's probe-once-per-ended-job decision. `observe` is called with the
 * covering job whenever it may have changed (the deck's `$effect`); it starts
 * the probe the first time it sees a given run ended, and `probed(runId)` is
 * true once that probe has answered.
 *
 * The flags are plain fields, not `$state`: `observe` runs inside an effect,
 * which may not write reactive state. `onProbed` is how the deck learns the
 * answer arrived (it sets its own `$state`).
 */
/** One run of a job: a re-enqueue keeps the row's id and bumps `attempt`, so
 * an answer for attempt 1 says nothing about attempt 2 (Codex on #6819). A
 * job with no attempt is keyed on its id alone. */
export function separateRunId(job: { id: string; attempt?: number }): string {
	return job.attempt === undefined ? job.id : `${job.id}@${job.attempt}`;
}

/**
 * One look for a track's bundle, as the deck's probe runs it. `stem_load`
 * resolves once the deck has STARTED reloading its stems (the engine runs
 * the stem probe and decode in the background, so the deck's command queue
 * is not held for seconds), so the command returning says nothing yet about
 * the bundle. The look waits for the deck's stems to leave `loading` before
 * it answers, and answers only for the track the deck still holds (Codex on
 * #6819).
 */
export async function lookForBundle(look: {
	/** Send the `stem_load`; true when it ran. */
	run: () => Promise<boolean>;
	/** Resolves once the deck's stems are no longer loading. */
	settled: () => Promise<void>;
	/** The deck still holds the track this look is for. */
	holds: () => boolean;
}): Promise<boolean> {
	if (!(await look.run())) return false;
	await look.settled();
	return look.holds();
}

export class SeparateEndProbe {
	/** Keys (`<stableId> <jobId>`) probed and answered. Sets, not one slot:
	 * a deck that moves from track A to B with both probes pending must keep
	 * B answerable whichever answers last. One short string per ended job a
	 * deck has shown, for the deck's lifetime. */
	readonly #started = new Set<string>();
	readonly #answered = new Set<string>();
	/** Keys whose `stem_load` did not run: no answer about the bundle. */
	readonly #failed = new Set<string>();
	/** What a probe that rejected threw, by key, until it is retried. */
	readonly #errors = new Map<string, unknown>();
	/** Resolves true only when the `stem_load` command actually ran for the
	 * track it is given (a deck that moved on to another track answers false). */
	readonly #probe: (stableId: string) => Promise<boolean>;
	readonly #onProbed: (runId: string) => void;

	constructor(probe: (stableId: string) => Promise<boolean>, onProbed: (runId: string) => void = () => {}) {
		this.#probe = probe;
		this.#onProbed = onProbed;
	}

	/** Start the probe for the deck's track `stableId` if its covering `job`
	 * has ended and was not probed for this track yet. Returns the probe's
	 * promise when one started, else null. */
	observe(stableId: string, job: { id: string; status: string; attempt?: number } | null): Promise<void> | null {
		if (job === null || !separateJobEnded(job.status)) return null;
		const runId = separateRunId(job);
		const key = `${stableId} ${runId}`;
		if (this.#started.has(key)) return null;
		this.#started.add(key);
		this.#failed.delete(key);
		this.#errors.delete(key);
		// Only a `stem_load` that RAN answers "is the bundle there"; a refused
		// one (which reports itself, toast) says nothing about it.
		const settled = (ran: boolean): void => {
			if (ran) this.#answered.add(key);
			else this.#failed.add(key);
			this.#onProbed(runId);
		};
		// The deck's probe resolves false for every command refusal, so a
		// rejection is a fault, not a refusal: the probe is still marked failed
		// (CHECK STEMS can look again) and the error is kept and rethrown, never
		// turned into a plain `false` (Sol on #6819).
		return this.#probe(stableId).then(settled, (exc: unknown) => {
			this.#errors.set(key, exc);
			settled(false);
			throw exc;
		});
	}

	/** Run a probe that did not run again (the deck's CHECK STEMS click). */
	retry(stableId: string, job: { id: string; status: string; attempt?: number } | null): Promise<void> | null {
		if (job === null || !this.failed(stableId, separateRunId(job))) return null;
		this.#started.delete(`${stableId} ${separateRunId(job)}`);
		return this.observe(stableId, job);
	}

	/** The probe for this track and ended run (`separateRunId`) has answered. */
	probed(stableId: string, runId: string): boolean {
		return this.#answered.has(`${stableId} ${runId}`);
	}

	/** The last probe for this track and ended job did not run. */
	failed(stableId: string, runId: string): boolean {
		return this.#failed.has(`${stableId} ${runId}`);
	}

	/** What the last probe for this track and ended run threw, or undefined
	 * when it resolved (answered or refused) or has not run. */
	error(stableId: string, runId: string): unknown {
		return this.#errors.get(`${stableId} ${runId}`);
	}
}
