/** Offloaded / local analysis + deck-load job progress for library UI.

Row bars, bottom ribbon, and context-menu kicks all read this store.
Process colors are fixed so agents and CSS stay aligned.

Display rules (classic progress bar):
- Determinate LTR fill via `--job-pct` (see TrackTable `.rb-row-job`).
- When only phase is known (queued/running/prefetch): asymptotic fake
  progress from ~2% toward FAKE_PROGRESS_CAP (~90%), never 100% until done.
- Real progress (upsert `real: true`) snaps the bar forward but still caps
  below 100% until phase `done` (then snap to 1 and clear).
- Stuck active jobs auto-clear after JOB_STUCK_TTL_MS with a console warn.

QA - analysis bar (real GPU path):
1. Open /performance, right-click a library row.
2. Choose "Run analyses…" -> enable Vocals -> Run.
3. Row wash fills LTR (blue = vocals); clears when cached/ready.

QA - without GPU (`?simulateJob=1` or Vite DEV):
- Right-click row -> "Simulate job bar", or
  `jobProgress.simulate('vocals', stableId, 3000)` / `window.__jobProgressSimulate(...)`.
*/
export type AnalysisKind =
	| 'vocals'
	| 'beatgrid'
	| 'key'
	| 'cues'
	| 'waveform'
	| 'phrase'
	| 'loudness'
	| 'stems'
	| 'lyrics'
	| 'load'
	| 'other';

/** Slot order for the 3x3 LHS dots (row-major). `load` is deck/audio only - not a coverage badge. */
export const ANALYSIS_DOT_SLOTS: readonly (AnalysisKind | null)[] = [
	'vocals',
	'beatgrid',
	'key',
	'cues',
	'waveform',
	'phrase',
	'loudness',
	'stems',
	'lyrics'
] as const;

/** Human-readable names for dot tooltips (funnel coverage + Err columns). */
export const ANALYSIS_LABELS: Record<AnalysisKind, string> = {
	vocals: 'Vocals',
	beatgrid: 'Beatgrid',
	key: 'Key',
	cues: 'Cues',
	waveform: 'Waveform',
	phrase: 'Phrase',
	loudness: 'Loudness',
	stems: 'Stems',
	lyrics: 'Lyrics',
	load: 'Load',
	other: 'Other'
};

export const ANALYSIS_COLORS: Record<AnalysisKind, string> = {
	vocals: '#5b8cff',
	beatgrid: '#3ecf8e',
	key: '#c9a227',
	cues: '#e87d3e',
	waveform: '#9b7bff',
	phrase: '#2ec4b6',
	loudness: '#f2545b',
	stems: '#adb5bd',
	lyrics: '#c77dff',
	load: '#4cc9f0',
	other: '#868e96'
};

/** Visible seed so a just-started bar is not an empty row. */
export const FAKE_PROGRESS_START = 0.02;
/** Asymptotic ceiling while waiting - never claim 100% before completion. */
export const FAKE_PROGRESS_CAP = 0.9;
/** Time constant for 1 - e^(-t/tau); ~few seconds to look busy, then crawls. */
export const FAKE_PROGRESS_TAU_MS = 8_000;
/** Single shared fake/TTL ticker (no rAF storms). */
export const FAKE_TICK_MS = 250;
/** Failsafe: clear stuck queued/running jobs. */
export const JOB_STUCK_TTL_MS = 45_000;

export type JobPhase = 'queued' | 'running' | 'done' | 'error';
export type AnalysisStatus = 'done' | 'missing' | 'queued' | 'in-progress';

export type TrackJob = {
	stable_id: string;
	kind: AnalysisKind;
	phase: JobPhase;
	/** Display 0..1. Fake ticks fill this when `real` is false. */
	progress: number;
	label?: string;
	updated_at: number;
	/** Wall clock when this job key first became active (fake elapsed base). */
	started_at: number;
	error?: string;
	/** When true, `progress` is authoritative (still capped until done). */
	real?: boolean;
};

export type AnalysisBadge = Partial<Record<AnalysisKind, boolean>>;

/** The popover's four honest states. An active job wins over stale badge data. */
export function analysisStatus(done: boolean, phase: JobPhase | null): AnalysisStatus {
	if (phase === 'running') return 'in-progress';
	if (phase === 'queued') return 'queued';
	return done ? 'done' : 'missing';
}

/** A real detected data-quality problem for one analysis kind (the browser
 * "Err" column) - never a guessed/fabricated severity. */
export type AnalysisIssue = {
	/** `warning` / `error`: a detected problem. `info`: a real property worth
	 * knowing that is not a defect (a variable-tempo grid). `unknown`: the
	 * detector could not judge this track, which is neither ok nor a problem
	 * and is drawn as a hollow dot. */
	severity: 'warning' | 'error' | 'info' | 'unknown';
	detail: string;
};
export type AnalysisIssues = Partial<Record<AnalysisKind, AnalysisIssue>>;
export const ANALYSIS_ISSUE_COLORS: Record<AnalysisIssue['severity'], string> = {
	warning: '#e8973e',
	error: '#e5484d',
	info: '#8a94a3',
	unknown: '#6b7480'
};

export type UpsertJobInput = Omit<TrackJob, 'updated_at' | 'started_at' | 'progress'> & {
	progress?: number;
	updated_at?: number;
	started_at?: number;
	real?: boolean;
};

// ----- pure helpers (unit-tested; also used by TrackTable / ribbon) -----

/** Floor 2% so a just-started bar is visible; clamp to 0..1. */
export function clampJobProgress(progress: number): number {
	if (!Number.isFinite(progress)) return FAKE_PROGRESS_START;
	return Math.max(FAKE_PROGRESS_START, Math.min(1, progress));
}

/**
 * Classic asymptotic fake progress: start + (cap - start) * (1 - e^(-t/tau)).
 * Never reaches 1; capped at `cap` (default ~0.9).
 */
export function fakeProgressAt(
	elapsedMs: number,
	opts?: { start?: number; cap?: number; tauMs?: number }
): number {
	const start = opts?.start ?? FAKE_PROGRESS_START;
	const cap = opts?.cap ?? FAKE_PROGRESS_CAP;
	const tauMs = opts?.tauMs ?? FAKE_PROGRESS_TAU_MS;
	if (!(cap > start)) return start;
	if (!Number.isFinite(elapsedMs) || elapsedMs <= 0) return start;
	const t = elapsedMs / Math.max(1, tauMs);
	const p = start + (cap - start) * (1 - Math.exp(-t));
	return Math.min(cap, Math.max(start, p));
}

export function isJobStuck(
	job: Pick<TrackJob, 'phase' | 'started_at' | 'kind'>,
	now = Date.now(),
	ttlMs = JOB_STUCK_TTL_MS
): boolean {
	if (job.kind === 'stems' || job.kind === 'lyrics') return false;
	if (job.phase !== 'queued' && job.phase !== 'running') return false;
	return now - job.started_at >= ttlMs;
}

export function phaseRank(phase: JobPhase): number {
	if (phase === 'running') return 2;
	if (phase === 'queued') return 1;
	return 0;
}

/** Active job for a row bar: running > queued; newer wins ties. Skips done/error. */
export function pickActiveJob(jobs: Iterable<TrackJob>, stableId: string): TrackJob | null {
	let best: TrackJob | null = null;
	for (const job of jobs) {
		if (job.stable_id !== stableId) continue;
		if (job.phase === 'done' || job.phase === 'error') continue;
		if (best === null) {
			best = job;
			continue;
		}
		const dr = phaseRank(job.phase) - phaseRank(best.phase);
		if (dr > 0 || (dr === 0 && job.updated_at >= best.updated_at)) best = job;
	}
	return best;
}

/** CSS custom props for the LTR row wash. */
export function jobRowStyleVars(job: Pick<TrackJob, 'kind' | 'progress'>): string {
	const pct = clampJobProgress(job.progress) * 100;
	return `--job-color:${ANALYSIS_COLORS[job.kind]}; --job-pct:${pct}%`;
}

export function ribbonFromJobs(
	jobs: Iterable<TrackJob>
): { kind: AnalysisKind; label: string; progress: number; n: number } | null {
	const active = [...jobs].filter((j) => j.phase === 'queued' || j.phase === 'running');
	if (active.length === 0) return null;
	const kind = active[0].kind;
	const same = active.filter((j) => j.kind === kind);
	const progress =
		same.reduce((s, j) => s + (j.phase === 'running' ? clampJobProgress(j.progress) : FAKE_PROGRESS_START), 0) /
		same.length;
	return {
		kind,
		label: `${kind} ${same.filter((j) => j.phase === 'running').length}/${same.length}`,
		progress,
		n: same.length
	};
}

/** DEV build or `?simulateJob=1` - gates QA simulate helper (no prod spam). */
export function jobSimulateAllowed(
	search = typeof globalThis !== 'undefined' &&
		typeof (globalThis as { location?: { search?: string } }).location?.search === 'string'
		? (globalThis as { location: { search: string } }).location.search
		: ''
): boolean {
	try {
		if (import.meta.env?.DEV) return true;
	} catch {
		/* bundler without import.meta.env */
	}
	return new URLSearchParams(search).get('simulateJob') === '1';
}

// ----- reactive store -----

let jobs = $state<Record<string, TrackJob>>({});
let badges = $state<Record<string, AnalysisBadge>>({});
let _tickTimer: ReturnType<typeof setInterval> | null = null;
const _simTimers = new Map<string, ReturnType<typeof setInterval>>();

function _jobKey(kind: AnalysisKind, stableId: string): string {
	return `${kind}:${stableId}`;
}

function _hasActiveJobs(): boolean {
	for (const job of Object.values(jobs)) {
		if (job.phase === 'queued' || job.phase === 'running') return true;
	}
	return false;
}

function _stopTicker(): void {
	if (_tickTimer === null) return;
	clearInterval(_tickTimer);
	_tickTimer = null;
}

function _ensureTicker(): void {
	if (_tickTimer !== null || !_hasActiveJobs()) return;
	_tickTimer = setInterval(_onTick, FAKE_TICK_MS);
}

function _onTick(): void {
	sweepStuck();
	const now = Date.now();
	let changed = false;
	const next = { ...jobs };
	for (const [key, job] of Object.entries(next)) {
		if (job.phase !== 'queued' && job.phase !== 'running') continue;
		if (job.real) continue;
		const p = fakeProgressAt(now - job.started_at);
		if (Math.abs(p - job.progress) < 0.0005) continue;
		next[key] = { ...job, progress: p, updated_at: now };
		changed = true;
	}
	if (changed) jobs = next;
	if (!_hasActiveJobs()) _stopTicker();
}

/** Clear stuck active jobs (TTL failsafe). Returns cleared keys. */
export function sweepStuck(now = Date.now(), ttlMs = JOB_STUCK_TTL_MS): string[] {
	const cleared: string[] = [];
	let next: Record<string, TrackJob> | null = null;
	for (const [key, job] of Object.entries(jobs)) {
		if (!isJobStuck(job, now, ttlMs)) continue;
		if (next === null) next = { ...jobs };
		delete next[key];
		cleared.push(key);
		console.warn(
			`[job-progress] clearing stuck ${job.kind} job for ${job.stable_id} after ${ttlMs}ms`
		);
	}
	if (next !== null) {
		jobs = next;
		if (!_hasActiveJobs()) _stopTicker();
	}
	return cleared;
}

function _bindSimulateGlobal(): void {
	if (typeof window === 'undefined') return;
	if (!jobSimulateAllowed()) return;
	(
		window as unknown as {
			__jobProgressSimulate?: (
				kind: AnalysisKind,
				stableId: string,
				ms?: number
			) => void;
		}
	).__jobProgressSimulate = (kind, stableId, ms) => {
		jobProgress.simulate(kind, stableId, ms);
	};
}

export const jobProgress = {
	get jobs() {
		return jobs;
	},
	get badges() {
		return badges;
	},
	upsert(job: UpsertJobInput): void {
		const key = _jobKey(job.kind, job.stable_id);
		const prev = jobs[key];
		const now = Date.now();
		const started_at = prev?.started_at ?? job.started_at ?? now;
		const phase = job.phase;
		let real = job.real === true;
		let progress: number;

		if (phase === 'done') {
			progress = 1;
			real = true;
		} else if (phase === 'error') {
			progress =
				job.progress !== undefined && Number.isFinite(job.progress)
					? Math.max(0, Math.min(1, job.progress))
					: (prev?.progress ?? 0);
		} else if (real) {
			const raw =
				job.progress !== undefined && Number.isFinite(job.progress)
					? job.progress
					: (prev?.progress ?? FAKE_PROGRESS_START);
			progress = Math.max(FAKE_PROGRESS_START, Math.min(FAKE_PROGRESS_CAP, raw));
		} else {
			const seed =
				job.progress !== undefined && Number.isFinite(job.progress)
					? Math.max(FAKE_PROGRESS_START, Math.min(FAKE_PROGRESS_CAP, job.progress))
					: fakeProgressAt(now - started_at);
			progress = Math.max(seed, fakeProgressAt(now - started_at));
		}

		jobs = {
			...jobs,
			[key]: {
				stable_id: job.stable_id,
				kind: job.kind,
				phase,
				progress,
				...(job.label === undefined ? {} : { label: job.label }),
				...(job.error === undefined ? {} : { error: job.error }),
				real,
				started_at,
				updated_at: job.updated_at ?? now
			}
		};
		if (phase === 'queued' || phase === 'running') _ensureTicker();
		else if (!_hasActiveJobs()) _stopTicker();
	},
	clear(stableId: string, kind?: AnalysisKind): void {
		const simKey = kind ? _jobKey(kind, stableId) : null;
		if (simKey !== null) {
			const t = _simTimers.get(simKey);
			if (t !== undefined) {
				clearInterval(t);
				_simTimers.delete(simKey);
			}
		} else {
			for (const [k, t] of _simTimers) {
				if (k.endsWith(`:${stableId}`)) {
					clearInterval(t);
					_simTimers.delete(k);
				}
			}
		}
		if (kind) {
			const key = _jobKey(kind, stableId);
			if (!(key in jobs)) return;
			const next = { ...jobs };
			delete next[key];
			jobs = next;
			if (!_hasActiveJobs()) _stopTicker();
			return;
		}
		const next = { ...jobs };
		let changed = false;
		for (const k of Object.keys(next)) {
			if (k.endsWith(`:${stableId}`)) {
				delete next[k];
				changed = true;
			}
		}
		if (changed) jobs = next;
		if (!_hasActiveJobs()) _stopTicker();
	},
	setBadge(stableId: string, kind: AnalysisKind, done: boolean): void {
		badges = {
			...badges,
			[stableId]: { ...(badges[stableId] ?? {}), [kind]: done }
		};
	},
	activeFor(stableId: string): TrackJob | null {
		return pickActiveJob(Object.values(jobs), stableId);
	},
	phaseFor(stableId: string, kind: AnalysisKind): JobPhase | null {
		const job = jobs[_jobKey(kind, stableId)];
		return job === undefined ? null : job.phase;
	},
	ribbon(): { kind: AnalysisKind; label: string; progress: number; n: number } | null {
		return ribbonFromJobs(Object.values(jobs));
	},
	/** Sweep TTL (also runs on the 4Hz ticker). */
	sweepStuck,
	/**
	 * DEV / `?simulateJob=1` only: upsert a fake job that ticks 0 -> 1 over `ms`,
	 * then clears. Default kind vocals, ~3s.
	 */
	simulate(kind: AnalysisKind, stableId: string, ms = 3_000): void {
		if (!jobSimulateAllowed()) {
			console.warn('[job-progress] simulate blocked (need DEV or ?simulateJob=1)');
			return;
		}
		if (stableId.length === 0) return;
		const dur = Math.max(200, ms);
		const key = _jobKey(kind, stableId);
		const prev = _simTimers.get(key);
		if (prev !== undefined) clearInterval(prev);
		const t0 = Date.now();
		jobProgress.upsert({
			stable_id: stableId,
			kind,
			phase: 'running',
			progress: FAKE_PROGRESS_START,
			label: 'simulate',
			real: true,
			started_at: t0
		});
		const timer = setInterval(() => {
			const elapsed = Date.now() - t0;
			const p = Math.min(1, elapsed / dur);
			if (p >= 1) {
				clearInterval(timer);
				_simTimers.delete(key);
				jobProgress.upsert({
					stable_id: stableId,
					kind,
					phase: 'done',
					progress: 1,
					label: 'simulate',
					real: true
				});
				jobProgress.clear(stableId, kind);
				return;
			}
			jobProgress.upsert({
				stable_id: stableId,
				kind,
				phase: 'running',
				progress: Math.min(FAKE_PROGRESS_CAP, Math.max(FAKE_PROGRESS_START, p)),
				label: 'simulate',
				real: true
			});
		}, FAKE_TICK_MS);
		_simTimers.set(key, timer);
	}
};

_bindSimulateGlobal();
