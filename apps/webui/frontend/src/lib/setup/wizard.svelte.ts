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
 */

import type { Job } from '../rb/jobs-store.svelte';
import {
	detectRekordbox,
	folderIsImportable,
	getSetupStatus,
	isFatalBlocker,
	scanFolder,
	setDismissed,
	setupRefusal,
	startFolderImport,
	startImport,
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

/** The job kind the import runs as. Mirrors SETUP_IMPORT_KIND. */
export const SETUP_IMPORT_KIND = 'setup.import-rekordbox';

const TERMINAL = ['succeeded', 'failed', 'cancelled', 'unknown'];

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

/** Fatal blockers only. A missing share dir is reported, never a stopper. */
export function fatalBlockers(detection: RekordboxDetection | null): string[] {
	if (detection === null) return [];
	return (detection.blockers ?? []).filter(isFatalBlocker);
}

export interface AdvanceContext {
	source: ImportSource;
	detection: RekordboxDetection | null;
	folderScan: FolderScan | null;
	job: Job | null;
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
	if (step === 'detect' && ctx.source === 'folder') {
		if (ctx.folderScan === null) return 'no folder has been checked yet';
		if (ctx.folderScan.denied) {
			return 'macOS is blocking that folder; grant access and check again';
		}
		if (!folderIsImportable(ctx.folderScan)) {
			return `nothing importable in ${ctx.folderScan.path}`;
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
	/** rekordbox by default; 'folder' is the no-rekordbox branch. */
	source = $state<ImportSource>('rekordbox');
	status = $state<SetupStatus | null>(null);
	detection = $state<RekordboxDetection | null>(null);
	/** The folder the operator typed, and what the daemon found in it. */
	folderPath = $state('');
	folderScan = $state<FolderScan | null>(null);
	/** The id of the job this wizard started. The row itself lives in
	 * jobsStore; duplicating it here would give the UI two truths. */
	jobId = $state<string | null>(null);
	busy = $state(false);
	error = $state<string | null>(null);

	goTo(step: WizardStep): void {
		this.step = step;
		this.error = null;
	}

	next(): void {
		this.goTo(nextStep(this.step));
	}

	back(): void {
		this.goTo(previousStep(this.step));
	}

	/** Load status + detection. Called when the wizard opens. */
	async load(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
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
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Re-run detection on demand, for the "I have fixed it, look again" case. */
	async redetect(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		this.busy = true;
		try {
			this.detection = await detectRekordbox();
			this.error = null;
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Switch branch. The previous branch's findings are dropped rather than
	 * left on screen describing something the operator is no longer doing. */
	useSource(source: ImportSource): void {
		this.source = source;
		this.error = null;
		if (source === 'rekordbox') this.folderScan = null;
	}

	/** Look inside the typed folder. Never imports anything. */
	async checkFolder(path: string): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		const trimmed = path.trim();
		if (trimmed === '') {
			this.error = 'type a folder path first';
			return;
		}
		this.busy = true;
		try {
			this.folderPath = trimmed;
			this.folderScan = await scanFolder(trimmed);
			this.error = null;
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Enqueue the folder import, advancing only once the server accepted it. */
	async beginFolderImport(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		if (!folderIsImportable(this.folderScan)) {
			this.error = 'check a folder with audio files in it first';
			return;
		}
		this.busy = true;
		try {
			const job = await startFolderImport({ folders: [this.folderPath] });
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
	async skip(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		this.busy = true;
		try {
			this.status = await setDismissed(true);
			this.error = null;
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Re-arm the wizard from settings. The inverse of skip(). */
	async reopen(): Promise<void> {
		const refusal = setupRefusal();
		if (refusal !== null) {
			this.error = refusal;
			return;
		}
		this.busy = true;
		try {
			this.status = await setDismissed(false);
			this.step = 'welcome';
			this.error = null;
		} catch (exc) {
			this.error = _message(exc);
		} finally {
			this.busy = false;
		}
	}

	/** Drop everything, for tests. */
	_resetForTests(): void {
		this.step = 'welcome';
		this.source = 'rekordbox';
		this.status = null;
		this.detection = null;
		this.folderPath = '';
		this.folderScan = null;
		this.jobId = null;
		this.busy = false;
		this.error = null;
	}
}

/** The one setup wizard store. */
export const setupWizard = new SetupWizard();
