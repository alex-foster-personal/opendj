/**
 * First-run wizard state: which step, what the daemon said, which job to watch.
 *
 * Rune module - the .svelte.ts extension is REQUIRED for $state. Only $state
 * is used (no $derived/$effect) so the node:test harness can bundle it, the
 * same constraint jobs-store.svelte.ts documents. Everything derived is a pure
 * exported function instead, which is also what makes the step rules testable
 * without mounting a component.
 *
 * There is no fabricated progress here and no optimistic step advance. The
 * progress step reads the real job row out of jobsStore; if the engine has said
 * nothing about that job yet, the step shows that it has said nothing.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 advanceRefusal(step, ctx): one function that both gates Next and
 *     supplies the tooltip, so a disabled button cannot disagree with why.
 *     [if] Next is enabled on 'detect' with a fatal blocker [then ⛔️] broken
 *   ✔︎ 🎯 beginImport() moves to 'progress' only after the server accepted the
 *     job, and records the id it returned.
 *     [if] the step advances on a 409 refusal [then ⛔️] broken
 *   ✔︎ 🎯 skip() dismisses engine-side, so the answer survives a reload and an
 *     agent sees it too.
 *     [if] the skip is only local state [then ⛔️] broken
 *   ✔︎ 🎯 every load path records the server's message on failure and leaves
 *     the previous data alone.
 *     [if] a failed refresh blanks an already-rendered detection [then ⛔️] broken
 *   ✔︎ 🎯 beginFolderImport() posts every validated non-empty folder row, not
 *     only the first.
 *     [if] two importable rows exist [then] folders[] has both [else ⛔️] broken
 *   ✔︎ 🎯 finish() re-reads preflight after the dismissal, so the root layout's
 *     empty-library gate judges the library as it is now, not as it was at boot.
 *     [if] the boot reading said library-attached 'fail' and the import has
 *     since run [then] the gate no longer asks for setup [else ⛔️] broken
 *     [if] the fresh reading still says 'fail' [then] finish() refuses with
 *     the engine's detail on `error` [else ⛔️] broken
 *   ✔︎ 🎯 refreshStatusAfterImport(jobId) re-reads setup status once per job
 *     id when this wizard's own import settles (the overlay calls it only for
 *     a terminal row), so the Done step reads the import that just ran, not
 *     the status loaded when the overlay opened.
 *     [if] the job succeeded and Done still says no import was recorded [then ⛔️] broken
 *     [if] another wizard's job id triggers a status read [then ⛔️] broken
 *   ✔︎ 🎯 load() and redetect() are bounded by `readTimeoutMs` (default
 *     SETUP_READ_TIMEOUT_MS): a stalled request is aborted and ends 'failed'
 *     with a plain sentence, never a scanning state with no end.
 *     [if] a read that never answers leaves detectState 'scanning' [then ⛔️] broken
 *   ✔︎ 🎯 ensureLoaded() re-runs load() only for the boot race (the daemon was
 *     not identified yet), never after the engine answered with a failure.
 *     [if] a failed status read or a failed Look again is retried by
 *     ensureLoaded() [then ⛔️] broken: the retry loops (Get started stays
 *     disabled) or repaints the old answer over the failure (Mac check of
 *     2f449f863, Fri 2 Oct 2026)
 */

import { capabilities } from '../api/capabilities.svelte';
import { checkPreflight } from '../preflight/preflight.svelte';
import type { Job } from '../rb/jobs-store.svelte';
import {
	detectRekordbox,
	finalSetupRefusal,
	getFolderCandidates,
	getSetupStatus,
	normalizeSetupFolderPath,
	scanFolder,
	setDismissed,
	setupRefusal,
	startFolderImport,
	startImport,
	type FolderCandidates,
	type RekordboxDetection,
	type SetupStatus,
} from './setup-api';
import {
	importableFolderPathsFromRows,
	newFolderRow,
	type FolderRow,
} from './folder-rows';
import { agentApiError, humanApiError, humanSetupReadError } from './present';
import {
	SETUP_READ_TIMEOUT_MS,
	SetupReadTimeout,
	bounded,
	errorMessage,
	finishBlocker
} from './setup-read';
import {
	backRefusal,
	nextStepFor,
	previousStepFor,
	type ImportSource,
	type ImportSourceSelection,
	type WizardStep,
} from './wizard-rules';

export * from './wizard-rules';
export { SETUP_READ_TIMEOUT_MS } from './setup-read';

class SetupWizard {
	step = $state<WizardStep>('welcome');
	/** Unselected until the operator picks a branch. STANDALONE-08. */
	source = $state<ImportSourceSelection>(null);
	status = $state<SetupStatus | null>(null);
	detection = $state<RekordboxDetection | null>(null);
	/** Folder-import rows: path input plus the daemon scan for that path. */
	folderRows = $state<FolderRow[]>([newFolderRow()]);
	/** The id of the job this wizard started. The row itself lives in
	 * jobsStore; duplicating it here would give the UI two truths. */
	jobId = $state<string | null>(null);
	busy = $state(false);
	error = $state<string | null>(null);
	/** Raw diagnostic for agents when `error` was sanitized for display. */
	errorDiagnostic = $state<string | null>(null);
	/**
	 * Whether detection has ever answered, tracked separately from the answer
	 * itself.
	 *
	 * `detection === null` used to mean three different things -- never asked,
	 * asking now, and asked-and-failed -- and the detect step painted all three
	 * as one grey "Looking..." that nothing would ever clear. A first-run user
	 * on a healthy machine read that as "no rekordbox found", which is the bug
	 * this field exists to make impossible.
	 */
	detectState = $state<'idle' | 'scanning' | 'answered' | 'failed'>('idle');
	folderCandidates = $state<FolderCandidates['candidates']>([]);
	folderCandidatesState = $state<'idle' | 'loading' | 'answered' | 'failed'>('idle');
	/** Job id for which setup status was re-read after success. */
	statusRefreshJobId = $state<string | null>(null);
	/** Last refreshStatusAfterImport failure for the current job, if any. */
	statusRefreshError = $state<string | null>(null);
	/** Deadline for load() and redetect(). A field so tests can shorten it. */
	readTimeoutMs = SETUP_READ_TIMEOUT_MS;
	/** True only when the last load() was refused because the daemon was not
	 * identified yet: the one failure ensureLoaded() may retry on its own. */
	private awaitingEngine = false;

	goTo(step: WizardStep): void {
		this.step = step;
		this.error = null;
		this.errorDiagnostic = null;
	}

	private _fail(message: string): void {
		this.errorDiagnostic = agentApiError(message);
		this.error = humanApiError(message);
	}

	/** A failed status or detection read: a fixed sentence for the operator,
	 * the raw message (plain-string details and paths included) for agents. */
	private _failRead(exc: unknown): void {
		this.errorDiagnostic = agentApiError(errorMessage(exc));
		this.error = humanSetupReadError(exc instanceof SetupReadTimeout);
		this.detectState = 'failed';
	}

	next(): void {
		this.goTo(nextStepFor(this.step, this.source));
	}

	/** Step backwards along THIS branch's route, or refuse and say why.
	 *
	 * The live job row is PASSED IN rather than read off this store, because
	 * the row lives in jobsStore and keeping a second copy here would give the
	 * UI two truths about one import. Callers that have no job pass nothing,
	 * which is exactly the "nothing is running" case.
	 *
	 * The refusal is recorded on `error` rather than swallowed: a Back that
	 * silently does nothing is the same dead control the wizard already had.
	 */
	back(job: Job | null = null): void {
		const why = backRefusal(this.step, {
			source: this.source,
			detection: this.detection,
			folderRows: this.folderRows,
			job
		});
		if (why !== null) {
			// 'this is the first step' is a statement of fact about a control
			// that should have been disabled, not an error to shout about.
			if (this.step !== 'welcome') this.error = why;
			return;
		}
		this.goTo(previousStepFor(this.step, this.source));
	}

	/**
	 * Load status + detection. Called when the wizard opens.
	 *
	 * The probe is awaited FIRST. `setupRefusal()` reads the capability store,
	 * which the root layout fills from one health GET in its own onMount; this
	 * mount runs in the same tick, so reading the refusal synchronously loses
	 * that race and pins "daemon not identified yet" on a perfectly healthy
	 * engine -- permanently, because nothing re-runs load(). Observed on the
	 * packaged app when /setup was opened directly. probe() is memoized on
	 * success, so this costs one request for the page, not one per call.
	 */
	async load(): Promise<void> {
		this.detectState = 'scanning';
		await capabilities.probe();
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			// FAILED, not "still looking". The caller re-runs load() once the
			// capability probe finally identifies an engine, so a daemon that
			// was merely slow to boot heals itself instead of stranding the
			// step on a scanning state nothing will ever clear.
			this.detectState = 'failed';
			this.awaitingEngine = true;
			return;
		}
		this.awaitingEngine = false;
		this.busy = true;
		try {
			this.status = await bounded('GET /api/v1/setup/status', this.readTimeoutMs, (signal) =>
				getSetupStatus(signal)
			);
			this.detection = this.status.rekordbox;
			this.error = null;
			this.errorDiagnostic = null;
			this.detectState = 'answered';
		} catch (exc) {
			this._failRead(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Re-run detection on demand, for the "I have fixed it, look again" case.
	 *
	 * Probes FIRST, like load() and runSetup(), because this button is also
	 * the manual retry for a daemon that was not up when the overlay mounted.
	 * Reading the refusal synchronously would answer a legitimate retry with a
	 * sentence about an unfinished health GET and change nothing. */
	async redetect(): Promise<void> {
		await capabilities.probe();
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			this.detectState = 'failed';
			return;
		}
		this.busy = true;
		this.detectState = 'scanning';
		try {
			this.detection = await bounded(
				'GET /api/v1/setup/detect/rekordbox',
				this.readTimeoutMs,
				(signal) => detectRekordbox(signal)
			);
			this.error = null;
			this.errorDiagnostic = null;
			this.detectState = 'answered';
		} catch (exc) {
			// The earlier answer stays on `detection`; 'failed' beside it is
			// what the detect step renders as "looking again did not finish".
			this._failRead(exc);
		} finally {
			this.busy = false;
		}
	}

	/**
	 * Load once, or re-load a previous attempt that could not reach the engine.
	 *
	 * THE BOOT RACE this closes: the overlay can mount in the same tick the
	 * root layout fires its one health GET, and in the packaged app the engine
	 * may not be listening yet at all. `load()` then returns early having
	 * asked nothing, and NOTHING re-ran it -- the detect step sat on a grey
	 * in-flight sentence permanently, on a machine where rekordbox was right
	 * there. Callers drive this from an effect on `capabilities.flavor`, so
	 * the retry is demand-driven off a state change and never a poll.
	 *
	 * ONLY that race is retried. A read that failed or timed out stays
	 * 'failed' until Try again or Look again: retrying it from the effect
	 * looped load() and repainted a failed Look again with the old answer.
	 */
	async ensureLoaded(): Promise<void> {
		if (this.detectState !== 'idle' && !this.awaitingEngine) return;
		if (this.detectState === 'scanning') return;
		if (finalSetupRefusal() !== null) return;
		await this.load();
	}

	/** Switch branch. The previous branch's findings are dropped rather than
	 * left on screen describing something the operator is no longer doing. */
	useSource(source: ImportSource): void {
		this.source = source;
		this.error = null;
		this.errorDiagnostic = null;
		if (source === 'rekordbox') this.resetFolderRows();
		if (source === 'folder') {
			this.resetFolderRows();
			void this.loadFolderCandidates();
		}
	}

	resetFolderRows(): void {
		this.folderRows = [newFolderRow()];
	}

	addFolderRow(): void {
		this.folderRows = [...this.folderRows, newFolderRow()];
	}

	removeFolderRow(id: string): void {
		if (this.folderRows.length <= 1) return;
		this.folderRows = this.folderRows.filter((row) => row.id !== id);
	}

	importableFolderPaths(): string[] {
		return importableFolderPathsFromRows(this.folderRows);
	}

	/** Load existing music folders worth suggesting. Fires once per wizard-open. */
	async loadFolderCandidates(): Promise<void> {
		if (
			this.folderCandidatesState === 'loading' ||
			this.folderCandidatesState === 'answered'
		) {
			return;
		}
		await capabilities.probe();
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			this.folderCandidatesState = 'failed';
			return;
		}
		this.folderCandidatesState = 'loading';
		try {
			const result = await getFolderCandidates();
			this.folderCandidates = result.candidates ?? [];
			this.error = null;
			this.folderCandidatesState = 'answered';
		} catch (exc) {
			this._fail(errorMessage(exc));
			this.folderCandidatesState = 'failed';
		}
	}

	/** Look inside the typed folder row. Never imports anything. */
	async checkFolderRow(id: string): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			return;
		}
		const row = this.folderRows.find((entry) => entry.id === id);
		if (row === undefined) return;
		const trimmed = row.path.trim();
		if (trimmed === '') {
			this._fail('type a folder path first');
			return;
		}
		const normalized = normalizeSetupFolderPath(trimmed);
		this.busy = true;
		try {
			const scan = await scanFolder(normalized);
			this.folderRows = this.folderRows.map((entry) =>
				entry.id === id ? { ...entry, path: normalized, scan } : entry
			);
			this.error = null;
			this.errorDiagnostic = null;
		} catch (exc) {
			this._fail(errorMessage(exc));
		} finally {
			this.busy = false;
		}
	}

	/** Fill the first empty row, or the last row when every row has a path. */
	applyFolderSuggestion(path: string): void {
		const empty = this.folderRows.find((row) => row.path.trim() === '');
		const target = empty ?? this.folderRows[this.folderRows.length - 1];
		if (target === undefined) return;
		this.folderRows = this.folderRows.map((row) =>
			row.id === target.id ? { ...row, path, scan: null } : row
		);
		void this.checkFolderRow(target.id);
	}

	/** Enqueue the folder import, advancing only once the server accepted it. */
	async beginFolderImport(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			return;
		}
		const folders = this.importableFolderPaths();
		if (folders.length === 0) {
			this._fail('check at least one folder with audio files in it');
			return;
		}
		this.busy = true;
		try {
			const job = await startFolderImport({ folders });
			this.jobId = job.id;
			this._clearStatusRefresh();
			this.error = null;
			this.errorDiagnostic = null;
			this.goTo('progress');
		} catch (exc) {
			this._fail(errorMessage(exc));
		} finally {
			this.busy = false;
		}
	}

	/** Enqueue the import and move to the progress step -- in that order.
	 *
	 * The step advances only after the server accepted the job, so a 409
	 * (another import already running, no rekordbox found) leaves the wizard on
	 * the confirm step with the server's own sentence on it.
	 */
	async beginImport(options: { refreshDecrypt?: boolean } = {}): Promise<void> {
		if (this.source !== 'rekordbox') {
			this._fail('choose rekordbox import before starting');
			return;
		}
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			return;
		}
		this.busy = true;
		try {
			const job = await startImport({
				refresh_decrypt: options.refreshDecrypt === true
			});
			this.jobId = job.id;
			this._clearStatusRefresh();
			this.error = null;
			this.errorDiagnostic = null;
			this.goTo('progress');
		} catch (exc) {
			this._fail(errorMessage(exc));
		} finally {
			this.busy = false;
		}
	}

	/** Skip the wizard. Persisted engine-side so a reload does not re-show it. */
	// STANDALONE-08: [if] user declines import [then] finish setup without re-offer, [else stop].
	async skip(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			return;
		}
		this.busy = true;
		try {
			this.status = await setDismissed(true);
			// Declining is final for this run: any door that reopens the overlay
			// without reopen() must still find a neutral wizard (Codex P2, #3561).
			this.source = null;
			this.error = null;
			this.errorDiagnostic = null;
		} catch (exc) {
			this._fail(errorMessage(exc));
		} finally {
			this.busy = false;
		}
	}

	/**
	 * Leave the wizard for good: dismiss engine-side, then RE-READ preflight,
	 * and say it is safe to close only once the engine's current answer agrees
	 * the library no longer needs setup. Every door that closes the overlay
	 * (Start playing, Skip for now, Continue without importing) goes through
	 * here.
	 *
	 * WHY THE RE-READ. The root layout re-raises this overlay whenever
	 * `needsSetupForEmptyLibrary(preflightGate.checks, open)` holds, and
	 * preflightGate is polled only while the boot gate is mounted, which it
	 * never is while setup is open. So the checks it held were the BOOT
	 * reading, taken over an empty library, and closing after a successful
	 * import made that effect raise the overlay again in the same tick.
	 * "Start playing" looked dead: the packaged preview on demon-llama, Thu 1
	 * Oct 2026, eleven POST /setup/dismiss all 200 and the wizard never left.
	 *
	 * A reading that still asks for setup is put on `error` rather than
	 * closing into a reopen nobody can see happen.
	 */
	async finish(): Promise<boolean> {
		await this.skip();
		if (this.error !== null) return false;
		// Held across the re-read so a second click cannot post a second
		// dismissal while the first is still being confirmed.
		this.busy = true;
		try {
			await checkPreflight();
		} finally {
			this.busy = false;
		}
		const blocker = finishBlocker();
		if (blocker !== null) {
			this.errorDiagnostic = blocker.diagnostic;
			this.error = blocker.human;
			return false;
		}
		return true;
	}

	/** Re-arm the wizard from settings. The inverse of skip(). */
	async reopen(): Promise<void> {
		// Same probe-before-refusal order as load(): this is the other door
		// into the wizard, and a click in the first tick after page load must
		// not be answered with a sentence about an unfinished health GET.
		await capabilities.probe();
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			return;
		}
		this.busy = true;
		try {
			this.status = await setDismissed(false);
			this.step = 'welcome';
			this.source = null;
			this.error = null;
			this.errorDiagnostic = null;
			// Re-arming is a fresh run: whatever detection said last time is
			// history, and ensureLoaded() must ask again rather than reuse it.
			this.detectState = 'idle';
			this._clearStatusRefresh();
		} catch (exc) {
			this._fail(errorMessage(exc));
		} finally {
			this.busy = false;
		}
	}

	/**
	 * Re-read setup status once THIS wizard's import job has settled, so Done
	 * shows the daemon's last_import, not the snapshot from when the overlay
	 * opened (issue #3422; the demon-llama preview, Thu 1 Oct 2026, said "No
	 * import was recorded for this data directory" after a good import).
	 *
	 * Once per job id: the overlay calls this on every update of a settled
	 * row, and a second call for the same id does not fetch again. A job id
	 * other than the one this wizard started is ignored, so someone else's
	 * import never rewrites this wizard's status. Until the read lands,
	 * advanceRefusal() holds Continue on the progress step; a failed read is
	 * kept on statusRefreshError (raw, for agents) and `error` (operator-safe).
	 */
	async refreshStatusAfterImport(jobId: string): Promise<void> {
		if (this.statusRefreshJobId === jobId) return;
		if (this.jobId !== null && jobId !== this.jobId) return;
		const refusal = setupRefusal();
		if (refusal !== null) {
			this._fail(refusal);
			this.statusRefreshError = refusal;
			return;
		}
		this.busy = true;
		this.statusRefreshError = null;
		try {
			const next = await getSetupStatus();
			this.status = next;
			this.detection = next.rekordbox;
			this.error = null;
			this.errorDiagnostic = null;
			this.statusRefreshJobId = jobId;
			this.statusRefreshError = null;
		} catch (exc) {
			const message = errorMessage(exc);
			this._fail(message);
			this.statusRefreshError = message;
		} finally {
			this.busy = false;
		}
	}

	_clearStatusRefresh(): void {
		this.statusRefreshJobId = null;
		this.statusRefreshError = null;
	}

	/** Drop everything, for tests. */
	_resetForTests(): void {
		this.step = 'welcome';
		this.source = null;
		this.status = null;
		this.detection = null;
		this.resetFolderRows();
		this.jobId = null;
		this.busy = false;
		this.error = null;
		this.errorDiagnostic = null;
		this.detectState = 'idle';
		this.awaitingEngine = false;
		this.readTimeoutMs = SETUP_READ_TIMEOUT_MS;
		this.folderCandidates = [];
		this.folderCandidatesState = 'idle';
		this._clearStatusRefresh();
	}
}

/** The one setup wizard store. */
export const setupWizard = new SetupWizard();
