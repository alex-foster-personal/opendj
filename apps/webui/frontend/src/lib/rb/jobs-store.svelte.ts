/**
 * Engine jobs: the live list behind the jobs drawer.
 *
 * Rune module - the .svelte.ts extension is REQUIRED for $state. Only $state
 * is used (no $derived/$effect) so the node:test harness can bundle it, the
 * same constraint pane-contract.svelte.ts documents.
 *
 * The engine's WS bus is an INVALIDATION bus, with one exception: the
 * `jobs.updated` payload IS the job row (apps/engine_core/jobs/store.py emits
 * the row it just wrote). So this store upserts that row directly instead of
 * refetching per event, which is what makes a progress bar move at worker
 * speed rather than at poll speed. Resync stays a full refetch, because a gap
 * means unknown rows changed and there is no replay to reconstruct them.
 *
 * There is no polling here and no fabricated progress. A row shows what the
 * engine last said about it; if the engine said nothing, the row shows
 * nothing. (Contrast job-progress.svelte.ts, the per-track analysis store,
 * which fakes an asymptotic bar for a UI with no real progress feed. These
 * are different surfaces and this one deliberately has no such path.)
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 hydrate(): GET /api/v1/jobs through the generated client, newest
 *     first. Failure lands in `error` and the list is left untouched.
 *     [if] a 500 empties an already-populated list [then ⛔️] broken
 *   ✔︎ 🎯 attach(bus): a `jobs.updated` frame upserts its row by id, and a
 *     resync refetches the whole list.
 *     [if] a second frame for one id grows the list to 2 [then ⛔️] broken
 *     [if] resync fires and no refetch is issued [then ⛔️] broken
 *   ✔︎ 🎯 cancel()/reenqueue(): mirror the server's guards, and a 409 refusal
 *     surfaces the server's own message verbatim.
 *     [if] a refusal is swallowed or reworded [then ⛔️] broken
 *   ✔︎ 🎯 hydrate()/attach() issue NOTHING when the serving daemon has no jobs
 *     API, and say why. The legacy daemon does not serve /api/v1/jobs, so a
 *     request there is a guaranteed 404 that teaches the user nothing.
 *     [if] a legacy boot fires a jobs request at all [then ⛔️] broken
 */

import type { components } from '../api-types';
import { jobsRefusal } from '../api/capabilities.svelte';
import { ApiError, api, unwrap } from '../api/client';
import { bootScheduler, type BootScheduler } from './boot-scheduler';
import {
	TOPIC_JOBS_UPDATED,
	subscribe,
	subscribeResync,
	type EventEnvelope,
	type ResyncReason,
	type Unsubscribe
} from '../api/events-bus';

export type Job = components['schemas']['JobOut'];

/** Mirrors STATUSES in apps/engine_core/jobs/store.py. A status outside this
 * list is contract drift, so it is reported rather than rendered as a chip
 * nobody can explain. */
export const JOB_STATUSES = [
	'queued',
	'running',
	'cancelling',
	'succeeded',
	'failed',
	'cancelled',
	'unknown'
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

/** Mirrors TERMINAL_STATUSES. Only these re-enqueue. */
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = [
	'succeeded',
	'failed',
	'cancelled',
	'unknown'
];

/** Matches DEFAULT_LIST_LIMIT in apps/engine_core/jobs/api.py. The server
 * refuses anything above MAX_LIST_LIMIT (1000) with a 422. */
export const JOBS_LIST_LIMIT = 200;

// ------------------------------------------------------------- predicates

/**
 * Why cancel is refused, or null when it is allowed.
 *
 * Mirrors `_assert_cancellable`: ONLY a running job cancels. Queued is not a
 * softer case of running here, it is refused outright, so the button must not
 * offer it. The engine is still the authority (the row can move between
 * render and click); this only decides what the UI claims is possible.
 */
export function cancelRefusal(job: Pick<Job, 'status'>): string | null {
	if (job.status === 'running') return null;
	if (job.status === 'cancelling') return 'already cancelling; waiting for the worker group to die';
	if (job.status === 'queued') {
		return 'queued jobs cannot be cancelled: the engine only cancels a job once a worker owns it';
	}
	return `job is ${job.status}; only a running job cancels`;
}

export function canCancel(job: Pick<Job, 'status'>): boolean {
	return cancelRefusal(job) === null;
}

/**
 * Why re-enqueue is refused, or null when it is allowed.
 *
 * Mirrors `_assert_requeueable`: only a terminal row re-enqueues. An
 * 'unknown' row is offered, but the server may still refuse it with a 409
 * when no reconcile hook can establish what the worker actually did. That
 * refusal is a real outcome, not a bug, and the drawer prints it.
 */
export function reenqueueRefusal(job: Pick<Job, 'status'>): string | null {
	if ((TERMINAL_JOB_STATUSES as readonly string[]).includes(job.status)) return null;
	return `job is ${job.status}; only a finished job re-enqueues`;
}

export function canReenqueue(job: Pick<Job, 'status'>): boolean {
	return reenqueueRefusal(job) === null;
}

export function isTerminal(job: Pick<Job, 'status'>): boolean {
	return (TERMINAL_JOB_STATUSES as readonly string[]).includes(job.status);
}

// --------------------------------------------------------------- display

/** Progress as a whole percent, clamped. The server sends 0..1. */
export function progressPct(progress: number): number {
	if (!Number.isFinite(progress)) return 0;
	return Math.round(Math.max(0, Math.min(1, progress)) * 100);
}

/**
 * The last few lines of a failure, newest information last.
 *
 * The engine puts a worker's stderr tail in `error`, which can be long. The
 * drawer shows the tail and carries the whole string in a `title`, so nothing
 * is lost, it is just not all on screen at once.
 */
export function errorTail(error: string | null | undefined, maxLines = 3): string {
	if (error === null || error === undefined) return '';
	const lines = error.split('\n').filter((line) => line.trim() !== '');
	return lines.slice(-maxLines).join('\n');
}

/** Local wall-clock for a server ISO timestamp. Empty string for null, so a
 * job that has not started renders blank rather than 'Invalid Date'. */
export function formatJobTime(iso: string | null | undefined): string {
	if (iso === null || iso === undefined || iso === '') return '';
	const at = new Date(iso);
	if (Number.isNaN(at.getTime())) return '';
	return at.toLocaleTimeString();
}

/** Newest first, id as the tiebreak so equal timestamps do not reorder on
 * every upsert (two jobs enqueued in the same millisecond is normal). */
export function compareJobs(a: Job, b: Job): number {
	if (a.created_at !== b.created_at) return a.created_at < b.created_at ? 1 : -1;
	return a.id < b.id ? 1 : -1;
}

// ----------------------------------------------------------------- frames

/**
 * Decode a `jobs.updated` payload into a job row, or null when it is off
 * contract.
 *
 * Shape-checked rather than trusted: the generated type is erased at runtime,
 * so it is a claim about the schema, never about the bytes on the socket. A
 * bad frame is logged and dropped instead of being spread into the list,
 * where a missing id would silently append a duplicate row on every update.
 */
export function readJobPayload(payload: Record<string, unknown>): Job | null {
	const { id, kind, status, progress, created_at } = payload;
	if (typeof id !== 'string' || id === '') {
		console.error('[jobs-store] jobs.updated payload has no string id', payload);
		return null;
	}
	if (typeof kind !== 'string' || typeof status !== 'string') {
		console.error(`[jobs-store] jobs.updated id=${id} has no string kind/status`, payload);
		return null;
	}
	if (typeof progress !== 'number' || !Number.isFinite(progress)) {
		console.error(`[jobs-store] jobs.updated id=${id} has a non finite progress`, payload);
		return null;
	}
	if (typeof created_at !== 'string') {
		console.error(`[jobs-store] jobs.updated id=${id} has a non string created_at`, payload);
		return null;
	}
	if (!(JOB_STATUSES as readonly string[]).includes(status)) {
		// Drift, not corruption: the engine grew a status this build predates.
		// Kept and rendered, because dropping it would hide a live job.
		console.error(`[jobs-store] jobs.updated id=${id} carries unknown status '${status}'`);
	}
	return payload as unknown as Job;
}

// ------------------------------------------------------------------ store

/** The slice of the events bus this store uses, as an injectable seam. The
 * real bus is the default; unit tests pass a fake and drive it by hand,
 * exactly as events-bus itself takes a socket factory. */
export interface JobsBus {
	subscribe: (topic: string, listener: (envelope: EventEnvelope) => void) => Unsubscribe;
	subscribeResync: (listener: (reason: ResyncReason) => void) => Unsubscribe;
}

const REAL_BUS: JobsBus = { subscribe, subscribeResync };

function _message(exc: unknown): string {
	if (exc instanceof ApiError) return exc.message;
	if (exc instanceof Error) return exc.message;
	return String(exc);
}

class JobsStore {
	/** Newest first. Only ever whole-array reassigned, so the identity change
	 * is what Svelte tracks and the node harness sees a plain array. */
	jobs = $state<Job[]>([]);
	/** True only while a list refetch is in flight. Per-row work uses busyId. */
	loading = $state(false);
	/** Last list-level failure, shown in the drawer. null = healthy. */
	error = $state<string | null>(null);
	/** The row with an action in flight, so its buttons disable without
	 * freezing the whole drawer. */
	busyId = $state<string | null>(null);
	/** Last per-row refusal, verbatim from the server. Cleared on the next
	 * action against the same row. */
	actionError = $state<{ id: string; message: string } | null>(null);
	/** Drawer visibility. Lives here so the TopBar toggle and the drawer share
	 * one source of truth, the midi-ui-state.svelte.ts pattern. */
	drawerOpen = $state(false);

	#detachers: Unsubscribe[] = [];
	/** How many components currently want the live subscription. See attach. */
	#holders = 0;

	/** Replace the list from the server. A failure leaves the previous rows
	 * on screen: a transient 500 must not look like "all your jobs vanished".
	 *
	 * Refuses BEFORE the request when the serving daemon has no jobs API. The
	 * gate lives here rather than only in the drawer so that no call site can
	 * reintroduce the 404 by calling the store directly. */
	async hydrate(): Promise<void> {
		const refusal = jobsRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		this.loading = true;
		try {
			const rows = await unwrap(
				api.GET('/api/v1/jobs', { params: { query: { limit: JOBS_LIST_LIMIT } } })
			);
			this.jobs = [...rows].sort(compareJobs);
			this.error = null;
		} catch (exc) {
			this.error = _message(exc);
			console.error('[jobs-store] hydrate failed', exc);
		} finally {
			this.loading = false;
		}
	}

	/** Insert or replace one row by id, keeping the newest-first order. */
	upsert(job: Job): void {
		const next = this.jobs.filter((row) => row.id !== job.id);
		next.push(job);
		this.jobs = next.sort(compareJobs);
	}

	/**
	 * Wire the store to the bus and do the first fetch. Returns a detach
	 * function; calling it twice is harmless.
	 *
	 * REFERENCE COUNTED, because more than one component now wants live jobs
	 * and they come and go independently: the drawer only while it is open,
	 * the TopBar's stems bar for the whole session. The single subscription is
	 * still created once (attaching twice would double every upsert and fire
	 * two refetches per resync), but it is torn down only when the LAST holder
	 * lets go. Without the count, closing the drawer detached the bus out from
	 * under the TopBar and its progress bar froze at whatever it last saw --
	 * a bar that stops moving being strictly worse than no bar.
	 *
	 * Each caller's detacher is idempotent, so a component that both returns
	 * it from an $effect and calls it on destroy still only releases once.
	 *
	 * A daemon with no jobs API also has no jobs.updated topic, so this
	 * subscribes to nothing and fetches nothing: it records why and hands back
	 * a detacher that has nothing to detach.
	 */
	attach(bus: JobsBus = REAL_BUS, scheduler: BootScheduler = bootScheduler): Unsubscribe {
		const refusal = jobsRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return () => undefined;
		}
		this.#holders += 1;
		if (this.#detachers.length === 0) {
			this.#detachers.push(
				bus.subscribe(TOPIC_JOBS_UPDATED, (envelope) => {
					const job = readJobPayload(envelope.payload);
					if (job === null) return;
					this.upsert(job);
				})
			);
			this.#detachers.push(
				bus.subscribeResync((reason) => {
					// A gap means rows changed in ways no frame described. Refetch
					// rather than trust what is on screen.
					//
					// 'initial-connect' is excluded: the bus's first-ever open now
					// fires a resync for that too (events-bus.ts, PR #1656 round 5),
					// but this store already schedules its own deferred initial
					// hydrate below through `scheduler.defer`, behind the PERF-R6
					// boot-window quiet period. Hydrating here as well on first open
					// would duplicate that fetch and bypass the window it exists to
					// enforce (PR #1656 review round 7, P2 BLOCKING).
					if (reason === 'initial-connect') return;
					void this.hydrate();
				})
			);
			// The SUBSCRIPTION is immediate -- a jobs.updated frame that
			// arrives during boot must not be missed. Only the catch-up
			// fetch waits for the boot window to clear (PERF-R6): it is a
			// 200-row list nobody is looking at yet, and the socket keeps
			// the rows current from the moment it is attached. An attach
			// after boot (the drawer opening) fetches immediately, because
			// the scheduler is already released by then.
			scheduler.defer('jobs-store:hydrate', () => {
				void this.hydrate();
			});
		}
		let released = false;
		return () => {
			if (released) return;
			released = true;
			this.release();
		};
	}

	/** Drop one holder, tearing the subscription down at zero. */
	release(): void {
		if (this.#holders === 0) return;
		this.#holders -= 1;
		if (this.#holders === 0) this.detach();
	}

	/** Unconditional teardown, regardless of holders. Tests and shutdown. */
	detach(): void {
		for (const off of this.#detachers) off();
		this.#detachers = [];
		this.#holders = 0;
	}

	/** running -> cancelling. A refusal is the server's sentence, unedited. */
	async cancel(id: string): Promise<void> {
		await this.#act(id, () =>
			unwrap(api.POST('/api/v1/jobs/{job_id}/cancel', { params: { path: { job_id: id } } }))
		);
	}

	/** terminal -> queued, attempt+1. May still be refused for an 'unknown'
	 * row that no reconcile hook can resolve. */
	async reenqueue(id: string): Promise<void> {
		await this.#act(id, () =>
			unwrap(api.POST('/api/v1/jobs/{job_id}/reenqueue', { params: { path: { job_id: id } } }))
		);
	}

	async #act(id: string, call: () => Promise<Job>): Promise<void> {
		this.busyId = id;
		this.actionError = null;
		try {
			this.upsert(await call());
		} catch (exc) {
			// 409 is the interesting one: the row was not in a state that
			// permits the transition. The engine explains why in the detail
			// message, so it is shown as-is rather than replaced by a generic
			// "action failed".
			this.actionError = { id, message: _message(exc) };
			console.error(`[jobs-store] action on job ${id} was refused`, exc);
		} finally {
			this.busyId = null;
		}
	}

	toggleDrawer(): void {
		this.drawerOpen = !this.drawerOpen;
	}

	closeDrawer(): void {
		this.drawerOpen = false;
	}
}

/** The one jobs store. */
export const jobsStore = new JobsStore();

/** TopBar toggle, mirroring toggleMidiPanel. */
export function toggleJobsDrawer(): void {
	jobsStore.toggleDrawer();
}
