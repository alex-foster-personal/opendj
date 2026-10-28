/**
 * "Separate this track" for a deck showing NO STEMS (STEM-77).
 *
 * Wiring only: whether a stems source answers on this machine comes from the
 * same plan endpoint the library prompt reads (GET /api/v1/stems/plan), and the
 * job is the per-track POST /api/v1/jobs {kind: stems.separate, stable_ids}, so
 * an agent reaches the same action over HTTP. A library-wide job
 * ({scope: 'pending'}, enqueued by the install prompt or `python -m apps.stems
 * separate --scope pending`) covers the track too while it is queued: its
 * worker resolves that scope, once at startup, to every library row with audio
 * on disk and no bundle (apps/stems/selection.py), which is exactly a deck
 * showing NO STEMS; once it runs it covers only tracks seen while it was
 * queued (`#scopeSeen`). Only
 * jobs for THIS engine's library count (`_sameLibrary`): a CLI job run with
 * `--data-dir /other/library` neither blocks nor claims a deck. The words live in
 * `separateActionView` (stem-separate-view.ts), which is unit tested.
 */
import { capabilities, jobsRefusal } from '$lib/api/capabilities.svelte';
import { type Job, jobsStore } from '$lib/rb/jobs-store.svelte';
import {
	STEMS_JOB_ACTIVE,
	STEMS_JOB_KIND,
	adoptableStemsJob,
	enqueueStemsForTracks,
	fetchStemsPlan
} from '$lib/rb/stems-jobs.svelte';
import { type SeparateActionView, separateActionView } from '$lib/rb/stem-separate-view';
import { separateJobEnded, separateRunId } from '$lib/rb/stem-separate-probe';

/** A source answer is re-read after this long, so a relay that comes up is noticed. */
const SOURCE_TTL_MS = 60_000;

/** Job statuses that mean a separation for the track is still under way. */
const ACTIVE_STATUSES: readonly string[] = ['queued', 'running', 'cancelling'];

/** `track` names this track in payload.stable_ids; `pending` is a library-wide
 * scope job whose run includes it. */
export type SeparateJobScope = 'track' | 'pending';

export interface SeparateJob {
	id: string;
	/** A re-enqueue keeps the row's id and bumps this, so a run is id + attempt. */
	attempt?: number;
	status: string;
	error?: string | null;
	scope: SeparateJobScope;
	/** False only for a QUEUED library-wide job nobody has asked the engine
	 * about for this track: it takes only tracks whose audio is on disk and
	 * that have no bundle when its worker starts, which this view cannot see,
	 * so a start is sent and the engine decides (Sol on #6819); the engine
	 * enqueues the track's own job beside it (apps/engine_core/jobs/stems_dedup.py
	 * covering_job, Codex on #6819). */
	confirmed: boolean;
}

export { STEMS_JOB_ACTIVE };

/** What one `separate()` call came to: the job it started (or, `adopted`, the
 * active job the engine said already does this work), or why it did not. */
export type SeparateOutcome =
	| { ok: true; job_id: string; adopted: boolean }
	| { ok: false; reason: string };

/** The serializable separation state a browser agent reads per deck
 * (PerformanceDeckSnapshot.stem_separation), the same inputs the button renders. */
export interface StemSeparationSnapshot {
	stable_id: string;
	/** `confirmed` is false only for a queued library-wide job the engine has
	 * not yet been asked about for this track (SeparateJob.confirmed). */
	job: { id: string; status: string; error: string | null; scope: SeparateJobScope; confirmed: boolean } | null;
	starting: boolean;
	enqueue_error: string | null;
	refusal: string | null;
	checking: boolean;
	/** Null with no ended covering job; else whether a `stem_load` that ran
	 * has looked for the bundle since that run ended. `stem_separate` is
	 * refused while it is false, as the deck's button is (CHECKING STEMS or
	 * CHECK STEMS): the run may already have written this track's stems. */
	bundle_checked: boolean | null;
}

/** A path spelled one way: forward slashes, no `.`/`..` segments, no
 * trailing separator. Lexical only; the engine resolves symlinks itself when
 * it dedupes (apps/engine_core/jobs/stems_dedup.py library_identity). */
function _lexicalPath(path: string): string {
	const slashed = path.replace(/\\/g, '/');
	const absolute = slashed.startsWith('/');
	const parts: string[] = [];
	for (const part of slashed.split('/')) {
		if (part === '' || part === '.') continue;
		if (part === '..' && parts.length > 0 && parts[parts.length - 1] !== '..') parts.pop();
		else parts.push(part);
	}
	return (absolute ? '/' : '') + parts.join('/');
}

/** This engine's data dir, from the library store its health names
 * (`<data_dir>/state/state.db`, the same derivation the server makes in
 * apps/webui/server/shell_output_health.py data_dir_from_state_db), or null
 * before the health probe has answered. */
export function engineLibraryDir(stateDbPath: string | null = capabilities.stateDbPath): string | null {
	if (stateDbPath === null) return null;
	const parts = _lexicalPath(stateDbPath).split('/');
	parts.pop();
	if (parts[parts.length - 1] === 'state') parts.pop();
	return parts.join('/') || '/';
}

/** Is this row's library the deck's? A payload with no data_dir runs on the
 * engine's own library; one naming a data_dir counts only when it is the
 * engine's. Before health has named the engine's library, a row naming one
 * is not counted: the engine's own duplicate check still refuses a real
 * duplicate with 409, which `separate()` adopts. */
function _sameLibrary(row: Job): boolean {
	const dataDir = (row.payload as { data_dir?: unknown } | null | undefined)?.data_dir;
	if (dataDir === undefined || dataDir === null) return true;
	const engine = engineLibraryDir();
	return typeof dataDir === 'string' && engine !== null && _lexicalPath(dataDir) === engine;
}

/** Does this engine job row separate `stableId`? Read from the row itself, so a
 * job enqueued over HTTP, by an agent, in another tab or before a reload counts. */
function _separatesTrack(row: Job, stableId: string): boolean {
	if (row.kind !== STEMS_JOB_KIND || !_sameLibrary(row)) return false;
	const ids = (row.payload as { stable_ids?: unknown } | null | undefined)?.stable_ids;
	return Array.isArray(ids) && ids.includes(stableId);
}

/** A library-wide stems job: payload.scope 'pending' and no stable_ids (the
 * server's canonical_payload drops whichever of the two was not given). */
function _isPendingScope(row: Job): boolean {
	if (row.kind !== STEMS_JOB_KIND || !_sameLibrary(row)) return false;
	const payload = row.payload as { scope?: unknown; stable_ids?: unknown } | null | undefined;
	return payload?.scope === 'pending' && payload.stable_ids === undefined;
}

/** The tier and executor a deck's start would run at, as the engine's plan
 * answer names them (its canonical payload resolves both the same way). */
export interface SeparateConfig {
	tier: string;
	executor: string;
}

/** The basename of a path in a worker argv. */
function _basename(part: string): string {
	return part.split(/[\\/]/).pop() ?? part;
}

/**
 * The tier and executor a SPAWNED worker runs at, read off its recorded argv
 * the way the engine reads it (stems_dedup._launched_as over
 * apps.stems.job.build_argv), or null for an argv it does not know.
 */
export function launchedConfig(argv: readonly unknown[]): SeparateConfig | null {
	const parts = argv.map(String);
	const flag = (name: string): string | undefined => {
		const at = parts.indexOf(name);
		return at >= 0 && at + 1 < parts.length ? parts[at + 1] : undefined;
	};
	const names = new Set(parts.map(_basename));
	const executor = flag('--executor');
	const tier = flag('--tier');
	if (names.has('stems_r2_first_worker.py') && executor !== undefined && tier !== undefined) return { tier, executor };
	if (names.has('stems_local_worker.py')) return { tier: 'LOCAL', executor: 'local' };
	if (names.has('stems_modal_worker.py') && tier !== undefined) return { tier, executor: 'modal' };
	return null;
}

/** Does this row run at `config`? The engine's duplicate check counts only a
 * row of the same tier and executor (stems_dedup.covering_job), so a row of
 * another one, or any row before the plan has named the deck's own, is not
 * known to make the stems this deck would ask for (Sol on #6819). A spawned
 * row runs what its recorded argv says, whatever its payload asked for, the
 * same rule the engine applies (stems_dedup._runs_as; Sol P1 on #6819). */
function _sameConfig(row: Job, config: SeparateConfig | null): boolean {
	if (config === null) return false;
	const argv = row.worker_argv;
	if (Array.isArray(argv)) {
		const launched = launchedConfig(argv);
		return launched !== null && launched.tier === config.tier && launched.executor === config.executor;
	}
	const payload = row.payload as { tier?: unknown; executor?: unknown } | null | undefined;
	return payload?.tier === config.tier && payload?.executor === config.executor;
}

/** When the row ended, for ordering ended rows; -Infinity when unknown. */
function _finishedAt(row: Job): number {
	const at = row.finished_at ? Date.parse(row.finished_at) : Number.NaN;
	return Number.isNaN(at) ? -Infinity : at;
}

class StemSeparateStore {
	/** Why separation cannot start, or null when it can; undefined until checked. */
	#sourceRefusal = $state<string | null | undefined>(undefined);
	/** The tier and executor the last good plan read named, or null. */
	#config = $state<SeparateConfig | null>(null);
	#checkedAt = 0;
	#inflight: Promise<void> | null = null;
	/** stable_id -> the last job id this session started for it. */
	#jobIds = $state<Record<string, string>>({});
	/** Ended runs (`<stableId> <runId>`, stem-separate-probe.ts) whose bundle a
	 * `stem_load` that ran has looked for since. */
	#bundleChecked = $state<Record<string, true>>({});
	/** stable_id -> why the last enqueue was refused, shown on the button. */
	#enqueueErrors = $state<Record<string, string>>({});
	/** stable_ids whose enqueue POST is in flight: a second click must not start a second job. */
	#starting = $state<Record<string, true>>({});
	/** stable_id -> scope job ids seen QUEUED while this track showed NO STEMS.
	 * Plain (not $state): job() is read inside $derived, which may not write
	 * state. A scope job's worker takes its list of pending tracks once, when
	 * it starts (scripts/stems_r2_first_worker.py), so only a scope job seen
	 * queued for this track is known to include it: it keeps covering the track
	 * while it runs and, once it finishes, still reloads this deck once. A scope
	 * job first seen already running, or already finished, does not claim the
	 * track (its list may predate the track), the same rule the engine applies
	 * (apps/engine_core/jobs/stems_dedup.py covering_job). */
	#scopeSeen = new Map<string, Set<string>>();

	get checking(): boolean {
		return this.#sourceRefusal === undefined && jobsRefusal() === null;
	}

	refusal(): string | null {
		return jobsRefusal() ?? this.#sourceRefusal ?? null;
	}

	/** Ask the plan endpoint once per TTL; a failed read is itself the refusal.
	 * Resolves when the check in flight (if any) has settled. */
	ensureChecked(now: number = Date.now()): Promise<void> {
		if (jobsRefusal() !== null) return Promise.resolve();
		if (this.#inflight !== null) return this.#inflight;
		if (this.#sourceRefusal !== undefined && now - this.#checkedAt < SOURCE_TTL_MS) return Promise.resolve();
		this.#inflight = (async () => {
			try {
				const plan = await fetchStemsPlan();
				this.#sourceRefusal = plan.local_refusal ?? plan.transport_refusal ?? null;
				this.#config = typeof plan.executor === 'string' ? { tier: plan.tier, executor: plan.executor } : null;
			} catch (exc) {
				this.#config = null;
				this.#sourceRefusal = `the stems plan could not be read (${exc instanceof Error ? exc.message : String(exc)})`;
			} finally {
				this.#checkedAt = Date.now();
				this.#inflight = null;
			}
		})();
		return this.#inflight;
	}

	/**
	 * The separation job for this track. Resolved from the engine's job rows by
	 * kind and payload.stable_ids, not only from ids this session started, so a
	 * job from the HTTP/agent path, another tab or before a page reload still
	 * disables the button and still reloads stems on success. A library-wide
	 * `scope: 'pending'` job counts while queued, and after that only if a deck
	 * showing NO STEMS saw it queued for this track (noteNoStems).
	 *
	 * Which row, when several match: an ACTIVE one (queued, running,
	 * cancelling) first: the job this session started or adopted, else the
	 * newest confirmed one (the deck's own tier and executor), else the newest
	 * unconfirmed one, so a newer run at another tier never hides an older one
	 * that makes this deck's stems, and a confirmed ended one beats an
	 * unconfirmed active one (Codex on #6819); else the ended one that FINISHED last,
	 * the job this session started or adopted winning a tie. Not simply the newest row: a
	 * re-enqueue keeps the row's original created_at, so an active re-run can
	 * sort below a newer attempt that already ended, and the deck must follow
	 * the run still under way, not the history. jobsStore.jobs is kept newest
	 * first.
	 */
	job(stableId: string): SeparateJob | null {
		const id = this.#jobIds[stableId];
		// The job this session started or adopted is the engine's own answer.
		// Any other row counts only at the deck's own tier and executor, and a
		// queued library job only once the engine says so.
		const confirmed = (row: Job): boolean =>
			row.id === id || (_sameConfig(row, this.#config) && !(_isPendingScope(row) && row.status === 'queued'));
		const view = (row: Job): SeparateJob => ({
			id: row.id,
			attempt: row.attempt,
			status: row.status,
			error: row.error ?? null,
			// An adopted job can be a scope job, so say what the row is.
			scope: _isPendingScope(row) ? 'pending' : 'track',
			confirmed: confirmed(row)
		});
		let activeConfirmed: Job | null = null;
		let activeOther: Job | null = null;
		let ended: Job | null = null;
		let endedAt = -Infinity;
		for (const row of jobsStore.jobs) {
			const own = row.id === id;
			let counts = own || _separatesTrack(row, stableId);
			if (!counts && _isPendingScope(row)) {
				counts = row.status === 'queued' || this.#scopeSeen.get(stableId)?.has(separateRunId(row)) === true;
			}
			if (!counts) continue;
			if (ACTIVE_STATUSES.includes(row.status)) {
				if (own) return view(row);
				if (confirmed(row)) activeConfirmed ??= row;
				else activeOther ??= row;
				continue;
			}
			// The LAST to finish, the remembered job included: another tab's
			// run that ended after it is the one whose stems the deck must
			// look for. On a tie (or no finished_at) the remembered job, else
			// list order (newest first).
			const at = _finishedAt(row);
			if (ended === null || at > endedAt || (own && at === endedAt)) {
				ended = row;
				endedAt = at;
			}
		}
		// A remembered job the list no longer holds (it fell out of the newest
		// JOBS_LIST_LIMIT rows) is not shown as queued: nothing says it is still
		// active, and a fabricated QUEUED would refuse every retry. A start or
		// adoption upserts its row (enqueueStemsForTracks, jobsStore.upsert), and
		// a refetch keeps rows upserted during it, so a live job is in the list.
		// A retry the engine sees as a duplicate is refused and adopted again
		// (Sol on #6819).
		// A confirmed run that ended also beats an unconfirmed one still under
		// way: the deck must probe the bundle it wrote, and the other run does
		// not make these stems, so it never refuses a start anyway (Codex on
		// #6819).
		const pick = activeConfirmed ?? (ended !== null && confirmed(ended) ? ended : (activeOther ?? ended));
		return pick === null ? null : view(pick);
	}

	/**
	 * The deck holding `stableId` shows NO STEMS: remember every library-wide
	 * job queued now as covering it, so it keeps covering the track once it
	 * runs (#scopeSeen). Called only from the NO STEMS paths (the deck's
	 * button, `stem_separate`), never from a lookup: a deck whose stems are
	 * ready is not in a scope worker's snapshot, so recording it there would
	 * let a running scope job refuse that track's separation after its bundle
	 * vanished (Sol on #6819).
	 */
	noteNoStems(stableId: string): void {
		for (const row of jobsStore.jobs) {
			if (row.status !== 'queued' || !_isPendingScope(row)) continue;
			const seen = this.#scopeSeen.get(stableId);
			if (seen === undefined) this.#scopeSeen.set(stableId, new Set([separateRunId(row)]));
			else seen.add(separateRunId(row));
		}
	}

	/** What the deck's SEPARATE button says for this track, given whether the
	 * deck has looked for the bundle since the job ended, or tried to and the
	 * look did not run (stem-separate-probe.ts). */
	actionView(stableId: string, probed: boolean, probeFailed = false): SeparateActionView {
		return separateActionView({
			refusal: this.refusal(),
			checking: this.checking,
			job: this.job(stableId),
			enqueueError: this.enqueueError(stableId),
			starting: this.starting(stableId),
			probed,
			probeFailed
		});
	}

	starting(stableId: string): boolean {
		return this.#starting[stableId] === true;
	}

	enqueueError(stableId: string): string | null {
		return this.#enqueueErrors[stableId] ?? null;
	}

	snapshot(stableId: string, deck: number): StemSeparationSnapshot {
		const job = this.job(stableId);
		return {
			stable_id: stableId,
			job: job === null ? null : { id: job.id, status: job.status, error: job.error ?? null, scope: job.scope, confirmed: job.confirmed },
			starting: this.starting(stableId),
			enqueue_error: this.enqueueError(stableId),
			refusal: this.refusal(),
			checking: this.checking,
			bundle_checked: this.bundleChecked(stableId, deck)
		};
	}

	/** A `stem_load` ran on `deck`, which holds `stableId` (performance-ipc):
	 * if the covering job has ended, that deck has now looked for its run's
	 * bundle. Nothing is recorded for a job still under way. The look is that
	 * deck's own: another deck holding the track may not have loaded what this
	 * one found, so its `stem_separate` still waits for its own `stem_load`
	 * (Codex on #6819). The look is credited to `run`, the ended run the load
	 * started under (endedRun, read before the reload began), so a load that
	 * began while the job was still under way and settled after it ended
	 * answers for no run, and a look under one attempt never counts for a
	 * later one (Codex P1 on #6819). */
	noteStemsLoaded(deck: number, stableId: string, run: string | null = this.endedRun(stableId)): void {
		if (run === null) return;
		this.#bundleChecked = { ...this.#bundleChecked, [`${deck} ${stableId} ${run}`]: true };
	}

	/** The covering run (separateRunId) if it has ended, else null: what a
	 * `stem_load` starting now can look for. */
	endedRun(stableId: string): string | null {
		const job = this.job(stableId);
		return job !== null && separateJobEnded(job.status) ? separateRunId(job) : null;
	}

	/** Null with no ended covering job; else whether a `stem_load` on `deck`
	 * has looked for that run's bundle since it ended (noteStemsLoaded). */
	bundleChecked(stableId: string, deck: number): boolean | null {
		const job = this.job(stableId);
		if (job === null || !separateJobEnded(job.status)) return null;
		return this.#bundleChecked[`${deck} ${stableId} ${separateRunId(job)}`] === true;
	}

	/**
	 * The `stem_separate` command (performance-ipc.svelte.ts) for the track on
	 * deck `deck`, whose stems read model shows `shown`: throws, for the
	 * command's error, when the deck has no track, does not show NO STEMS, has
	 * not looked for the bundle since the covering job ended, or the start was
	 * refused.
	 */
	async separateDeck(deck: number, stableId: string | null, shown: string): Promise<void> {
		if (stableId === null) throw new Error(`stem_separate: deck ${deck} has no track loaded`);
		if (shown !== 'none') {
			throw new Error(`stem_separate: deck ${deck} does not show NO STEMS (stems are ${shown})`);
		}
		this.noteNoStems(stableId);
		if (this.bundleChecked(stableId, deck) === false) {
			// The button's load-first rule (Codex on #6819): an ended run may
			// have written this track's bundle whatever its status, so nothing
			// is bought until a stem_load has looked for it.
			const ended = this.job(stableId);
			throw new Error(
				`stem_separate: separation job ${ended?.id} covering this track ended (${ended?.status}) and deck ${deck} has not looked for the stems it may have written; send stem_load for deck ${deck} first`
			);
		}
		const outcome = await this.separate(stableId);
		if (!outcome.ok) throw new Error(`stem_separate: ${outcome.reason}`);
	}

	/**
	 * Never throws: a refused start is recorded and the button says why, and the
	 * outcome tells a command caller (stem_separate) whether a job was started.
	 * A track with a separation already queued, running or cancelling is
	 * refused here rather than sent; the engine's own atomic check (409
	 * stems_job_active, adopted below) catches what this view cannot see.
	 */
	async separate(stableId: string): Promise<SeparateOutcome> {
		if (this.#starting[stableId]) {
			return { ok: false, reason: 'a separation request for this track is already being sent' };
		}
		const active = this.job(stableId);
		// A queued library job the engine has not confirmed for this track is
		// no reason to refuse: the POST below is the question, answered by a
		// 409 handing that job back (adopted) or by a job of its own.
		if (active !== null && ACTIVE_STATUSES.includes(active.status) && active.confirmed) {
			const what = active.scope === 'pending'
				? `a library-wide separation job (scope pending, which includes this track) is already ${active.status}`
				: `a separation job for this track is already ${active.status}`;
			return { ok: false, reason: `${what} (job ${active.id})` };
		}
		const { [stableId]: _cleared, ...rest } = this.#enqueueErrors;
		this.#enqueueErrors = rest;
		const blocked = this.refusal();
		if (blocked !== null) {
			const reason = `refusing to start separation: ${blocked}`;
			this.#enqueueErrors = { ...this.#enqueueErrors, [stableId]: reason };
			return { ok: false, reason };
		}
		if (this.#sourceRefusal === undefined) {
			// Only a COMPLETED, successful source check lets a job start: before
			// it answers, "no refusal" means "not asked yet", not "allowed".
			// Not recorded on the button, which already reads "Checking...".
			void this.ensureChecked();
			return {
				ok: false,
				reason: 'refusing to start separation: the stems source check (GET /api/v1/stems/plan) has not finished yet; try again when it has'
			};
		}
		this.#starting = { ...this.#starting, [stableId]: true };
		try {
			const job = await enqueueStemsForTracks([stableId]);
			this.#jobIds = { ...this.#jobIds, [stableId]: job.id };
			// The engine took the request, so no queued library job covers the
			// track: forget any it was seen beside, or a run that skips it would
			// keep refusing it once this job ends (Sol on #6819).
			this.#scopeSeen.delete(stableId);
			return { ok: true, job_id: job.id, adopted: false };
		} catch (exc) {
			// Another tab or agent won the race: the engine refused atomically
			// and handed back the job already doing this work. Adopt it (shown,
			// remembered, reloaded on success); it is not an error.
			const adopted = adoptableStemsJob(exc);
			if (adopted !== null) {
				jobsStore.upsert(adopted);
				this.#jobIds = { ...this.#jobIds, [stableId]: adopted.id };
				// The adopted job now covers the track, remembered by its id, so a
				// library job the track was seen beside no longer claims it: an
				// adopted per-track job must not leave a library run that skips
				// the track refusing its retry, and an adopted library job keeps
				// covering it as the remembered job (Sol on #6819).
				this.#scopeSeen.delete(stableId);
				return { ok: true, job_id: adopted.id, adopted: true };
			}
			const message = exc instanceof Error ? exc.message : String(exc);
			this.#enqueueErrors = { ...this.#enqueueErrors, [stableId]: message };
			return { ok: false, reason: message };
		} finally {
			const { [stableId]: _done, ...left } = this.#starting;
			this.#starting = left;
		}
	}
}

// The typed dispatcher and the deck load this store on demand, never by a
// static import, so it stays out of the initial load of "/"; see the port
// module beside this one, which this store does not import.
export const stemSeparate = new StemSeparateStore();
