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
 */

import { capabilities } from '../api/capabilities.svelte';
import type { Job } from '../rb/jobs-store.svelte';
import {
	detectRekordbox,
	finalSetupRefusal,
	folderIsImportable,
	getFolderCandidates,
	getSetupStatus,
	isFatalBlocker,
	normalizeSetupFolderPath,
	scanFolder,
	setDismissed,
	setupRefusal,
	startFolderImport,
	startImport,
	type FolderCandidates,
	type FolderScan,
	type RekordboxDetection,
	type SetupStatus,
} from './setup-api';

export const WIZARD_STEPS = [
	'welcome',
	'detect',
	'confirm',
	'progress',
	'stems',
	'done'
] as const;

export type WizardStep = (typeof WIZARD_STEPS)[number];

export const STEP_TITLES: Record<WizardStep, string> = {
	welcome: 'Welcome',
	detect: 'Find your music',
	confirm: 'Confirm the import',
	progress: 'Importing',
	stems: 'Stems analysis',
	done: 'Done'
};

/** Which library the wizard is importing FROM. */
export type ImportSource = 'rekordbox' | 'folder';

/** Unselected until the operator picks a branch. STANDALONE-08: detection alone
 * must not imply rekordbox. */
export type ImportSourceSelection = ImportSource | null;

/** The job kind the import runs as. Mirrors SETUP_IMPORT_KIND. */
export const SETUP_IMPORT_KIND = 'setup.import-rekordbox';

const TERMINAL = ['succeeded', 'failed', 'cancelled', 'unknown'];

/** One folder path row on the first-run folder-import step. */
export type FolderRow = {
	id: string;
	path: string;
	scan: FolderScan | null;
};

function _newFolderRow(): FolderRow {
	return { id: crypto.randomUUID(), path: '', scan: null };
}

// ------------------------------------------------------------- pure rules

export function stepIndex(step: WizardStep): number {
	return WIZARD_STEPS.indexOf(step);
}

export function nextStep(step: WizardStep): WizardStep {
	const index = stepIndex(step);
	return WIZARD_STEPS[Math.min(index + 1, WIZARD_STEPS.length - 1)];
}

export function previousStep(step: WizardStep): WizardStep {
	const index = stepIndex(step);
	return WIZARD_STEPS[Math.max(index - 1, 0)];
}

/**
 * The steps THIS branch will actually visit.
 *
 * 'confirm' confirms a rekordbox collection, so the folder branch never goes
 * near it: beginFolderImport() jumps straight from 'detect' to 'progress'.
 * That asymmetry was invisible while nothing could move backwards. The moment
 * Back exists, walking back from 'progress' on a folder import would land on a
 * rekordbox confirmation screen for a library the user is not importing, so
 * the route has to know which branch it is on.
 */
export function visibleSteps(source: ImportSourceSelection): WizardStep[] {
	if (source === 'folder' || source === null) {
		return WIZARD_STEPS.filter((step) => step !== 'confirm');
	}
	return [...WIZARD_STEPS];
}

/** 1-based position of `step` in this branch's route, for "step 2 of 5". */
export function stepPosition(step: WizardStep, source: ImportSourceSelection): number {
	return visibleSteps(source).indexOf(step) + 1;
}

/** How many steps this branch has in total. */
export function stepCount(source: ImportSourceSelection): number {
	return visibleSteps(source).length;
}

export function nextStepFor(step: WizardStep, source: ImportSourceSelection): WizardStep {
	const route = visibleSteps(source);
	const index = route.indexOf(step);
	if (index === -1) return step;
	return route[Math.min(index + 1, route.length - 1)];
}

export function previousStepFor(step: WizardStep, source: ImportSourceSelection): WizardStep {
	const route = visibleSteps(source);
	const index = route.indexOf(step);
	if (index === -1) return step;
	return route[Math.max(index - 1, 0)];
}

/**
 * Why Back is refused on this step, or null when it is allowed.
 *
 * Deliberately symmetric with advanceRefusal: one function both gates the
 * button and supplies its tooltip, so a disabled Back cannot disagree with
 * the reason shown for it.
 *
 * Only two things refuse. The first step has nothing behind it. And a LIVE
 * import cannot be walked away from: the job keeps running whatever the
 * wizard shows, so a user who stepped back to 'confirm' and pressed Start
 * again would be queueing a second import on top of the first. Once the job
 * reaches a terminal state that stops being true and Back opens up again,
 * which is what makes a failed import re-runnable instead of a dead end.
 */
export function backRefusal(step: WizardStep, ctx: AdvanceContext): string | null {
	if (step === 'welcome') return 'this is the first step';
	if (step === 'progress' && ctx.job !== null && !TERMINAL.includes(ctx.job.status)) {
		return `the import is running (${ctx.job.status}); it cannot be un-started by going back`;
	}
	return null;
}

/** Fatal blockers only. A missing share dir is reported, never a stopper. */
export function fatalBlockers(detection: RekordboxDetection | null): string[] {
	if (detection === null) return [];
	return (detection.blockers ?? []).filter(isFatalBlocker);
}

export interface AdvanceContext {
	source: ImportSourceSelection;
	detection: RekordboxDetection | null;
	folderRows: FolderRow[];
	job: Job | null;
}

/**
 * The row's scan, but only while it still describes the path in the text box.
 *
 * `bind:value` edits `row.path` in place and leaves `row.scan` alone, so a row
 * checked as /Music/A and then retyped as /Music/B would otherwise import B on
 * the strength of A's scan. The server echoes the path it scanned, normalized
 * the same way (`normalize_setup_folder_path`), so a mismatch means stale.
 */
export function currentFolderScan(row: FolderRow): FolderScan | null {
	if (row.scan === null) return null;
	return normalizeSetupFolderPath(row.path) === row.scan.path ? row.scan : null;
}

/** Non-empty folder rows that passed check and are importable, normalized and deduped. */
export function importableFolderPathsFromRows(rows: FolderRow[]): string[] {
	const paths: string[] = [];
	const seen = new Set<string>();
	for (const row of rows) {
		if (row.path.trim() === '' || !folderIsImportable(currentFolderScan(row))) continue;
		const canon = normalizeSetupFolderPath(row.path);
		if (seen.has(canon)) continue;
		seen.add(canon);
		paths.push(canon);
	}
	return paths;
}

/**
 * Why Next is refused on this step, or null when it is allowed.
 *
 * The progress step refuses while the job is still live on purpose: a wizard
 * that lets you walk past a running import is a wizard whose "done" screen is
 * a guess.
 *
 * On the detect step the refusal depends on which source is selected, and a
 * fatal rekordbox blocker must NOT block someone who has switched to a
 * folder -- that is the whole point of the folder branch.
 */
export function advanceRefusal(step: WizardStep, ctx: AdvanceContext): string | null {
	// STANDALONE-08: [if] RB install detected [then] import only after explicit act, [else stop].
	if (step === 'detect' && ctx.source === null) {
		return 'choose an import source first';
	}
	if (step === 'detect' && ctx.source === 'folder') {
		const nonEmpty = ctx.folderRows.filter((row) => row.path.trim() !== '');
		const anyChecked = ctx.folderRows.some((row) => currentFolderScan(row) !== null);
		if (!anyChecked) return 'no folder has been checked yet';

		for (const row of nonEmpty) {
			const scan = currentFolderScan(row);
			if (scan === null) return `check ${row.path.trim()} first`;
			if (scan.denied) {
				return 'macOS is blocking that folder; grant access and check again';
			}
			if (!folderIsImportable(scan)) {
				return `nothing importable in ${scan.path}`;
			}
		}

		const importable = importableFolderPathsFromRows(ctx.folderRows);
		if (importable.length === 0) return 'no folder has been checked yet';

		const normalized = nonEmpty
			.filter((row) => folderIsImportable(currentFolderScan(row)))
			.map((row) => normalizeSetupFolderPath(row.path));
		if (new Set(normalized).size !== normalized.length) {
			return 'remove duplicate folder paths before importing';
		}
		return null;
	}
	if (step === 'detect') {
		if (ctx.detection === null) return 'detection has not answered yet';
		const fatal = fatalBlockers(ctx.detection);
		if (fatal.length > 0) return `cannot import: ${fatal.join(', ')}`;
		return null;
	}
	if (step === 'progress') {
		if (ctx.job === null) return 'no import has been started yet';
		if (!TERMINAL.includes(ctx.job.status)) return `import is ${ctx.job.status}`;
		if (ctx.job.status !== 'succeeded') {
			return `import ${ctx.job.status}; re-run it before finishing`;
		}
		return null;
	}
	if (step === 'done') return 'this is the last step';
	return null;
}

/** Percent for a progress bar, clamped. Mirrors progressPct in jobs-store, but
 * this module must not import a UI helper from another surface just for one
 * arithmetic line. */
export function importPct(job: Job | null): number {
	if (job === null) return 0;
	return Math.max(0, Math.min(100, Math.round(job.progress * 100)));
}

function _message(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

// ------------------------------------------------------------------ store

class SetupWizard {
	step = $state<WizardStep>('welcome');
	/** Unselected until the operator picks a branch. STANDALONE-08. */
	source = $state<ImportSourceSelection>(null);
	status = $state<SetupStatus | null>(null);
	detection = $state<RekordboxDetection | null>(null);
	/** Folder-import rows: path input plus the daemon scan for that path. */
	folderRows = $state<FolderRow[]>([_newFolderRow()]);
	/** The id of the job this wizard started. The row itself lives in
	 * jobsStore; duplicating it here would give the UI two truths. */
	jobId = $state<string | null>(null);
	busy = $state(false);
	error = $state<string | null>(null);
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

	goTo(step: WizardStep): void {
		this.step = step;
		this.error = null;
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
			this.error = refusal;
			// FAILED, not "still looking". The caller re-runs load() once the
			// capability probe finally identifies an engine, so a daemon that
			// was merely slow to boot heals itself instead of stranding the
			// step on a scanning state nothing will ever clear.
			this.detectState = 'failed';
			return;
		}
		this.busy = true;
		try {
			// Sequential, not Promise.all: two requests whose second one is only
			// meaningful if the first succeeded, and a combined rejection would
			// lose which of them failed.
			this.status = await getSetupStatus();
			this.detection = this.status.rekordbox;
			this.error = null;
			this.detectState = 'answered';
		} catch (exc) {
			this.error = _message(exc);
			this.detectState = 'failed';
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
			this.error = refusal;
			this.detectState = 'failed';
			return;
		}
		this.busy = true;
		this.detectState = 'scanning';
		try {
			this.detection = await detectRekordbox();
			this.error = null;
			this.detectState = 'answered';
		} catch (exc) {
			this.error = _message(exc);
			this.detectState = 'failed';
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
	 */
	async ensureLoaded(): Promise<void> {
		if (this.detectState === 'answered' || this.detectState === 'scanning') return;
		if (finalSetupRefusal() !== null) return;
		await this.load();
	}

	/** Switch branch. The previous branch's findings are dropped rather than
	 * left on screen describing something the operator is no longer doing. */
	useSource(source: ImportSource): void {
		this.source = source;
		this.error = null;
		if (source === 'rekordbox') this.resetFolderRows();
		if (source === 'folder') {
			this.resetFolderRows();
			void this.loadFolderCandidates();
		}
	}

	resetFolderRows(): void {
		this.folderRows = [_newFolderRow()];
	}

	addFolderRow(): void {
		this.folderRows = [...this.folderRows, _newFolderRow()];
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
			this.error = refusal;
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
			this.error = _message(exc);
			this.folderCandidatesState = 'failed';
		}
	}

	/** Look inside the typed folder row. Never imports anything. */
	async checkFolderRow(id: string): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		const row = this.folderRows.find((entry) => entry.id === id);
		if (row === undefined) return;
		const trimmed = row.path.trim();
		if (trimmed === '') {
			this.error = 'type a folder path first';
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
		} catch (exc) {
			this.error = _message(exc);
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
			this.error = refusal;
			return;
		}
		const folders = this.importableFolderPaths();
		if (folders.length === 0) {
			this.error = 'check at least one folder with audio files in it';
			return;
		}
		this.busy = true;
		try {
			const job = await startFolderImport({ folders });
			this.jobId = job.id;
			this.error = null;
			this.goTo('progress');
		} catch (exc) {
			this.error = _message(exc);
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
			this.error = 'choose rekordbox import before starting';
			return;
		}
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		this.busy = true;
		try {
			const job = await startImport({
				refresh_decrypt: options.refreshDecrypt === true
			});
			this.jobId = job.id;
			this.error = null;
			this.goTo('progress');
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Skip the wizard. Persisted engine-side so a reload does not re-show it. */
	// STANDALONE-08: [if] user declines import [then] finish setup without re-offer, [else stop].
	async skip(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		this.busy = true;
		try {
			this.status = await setDismissed(true);
			// Declining is final for this run: any door that reopens the overlay
			// without reopen() must still find a neutral wizard (Codex P2, #3561).
			this.source = null;
			this.error = null;
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Re-arm the wizard from settings. The inverse of skip(). */
	async reopen(): Promise<void> {
		// Same probe-before-refusal order as load(): this is the other door
		// into the wizard, and a click in the first tick after page load must
		// not be answered with a sentence about an unfinished health GET.
		await capabilities.probe();
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		this.busy = true;
		try {
			this.status = await setDismissed(false);
			this.step = 'welcome';
			this.source = null;
			this.error = null;
			// Re-arming is a fresh run: whatever detection said last time is
			// history, and ensureLoaded() must ask again rather than reuse it.
			this.detectState = 'idle';
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
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
		this.detectState = 'idle';
		this.folderCandidates = [];
		this.folderCandidatesState = 'idle';
	}
}

/** The one setup wizard store. */
export const setupWizard = new SetupWizard();
