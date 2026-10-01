/**
 * The first-run wizard's step model and refusal rules: which steps a branch
 * visits, and why Next or Back is refused on a step. Pure functions only, so
 * the rules are executable under node:test without the store. Split out of
 * wizard.svelte.ts, which re-exports all of it; import from there.
 */

import type { Job } from '../rb/jobs-store.svelte';
import { folderAdvanceRefusal, type FolderRow } from './folder-rows';
import { humanAdvanceRefusal } from './present';
import { isFatalBlocker, type RekordboxDetection } from './setup-api';

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

export const TERMINAL = ['succeeded', 'failed', 'cancelled', 'unknown'];

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
	if (step === 'detect' && ctx.source === 'folder') return folderAdvanceRefusal(ctx.folderRows);
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

/** Operator-safe refusal copy for the current step. */
export function humanRefusal(step: WizardStep, ctx: AdvanceContext): string | null {
	return humanAdvanceRefusal(step, ctx, advanceRefusal(step, ctx));
}

/** Percent for a progress bar, clamped. Mirrors progressPct in jobs-store, but
 * this module must not import a UI helper from another surface just for one
 * arithmetic line. */
export function importPct(job: Job | null): number {
	if (job === null) return 0;
	return Math.max(0, Math.min(100, Math.round(job.progress * 100)));
}
