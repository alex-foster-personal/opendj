/**
 * PREFLIGHT-05: the startup/preflight screen explains its buttons inline and
 * acknowledges every click.
 *
 * Two problems on the boot screen (the maintainer, Mon 5 Oct 2026): the buttons'
 * explanations only existed as hover tooltips, which pop up and shift what
 * the user is looking at, and a click on "Re-check" or "Re-request
 * permissions" changed nothing visible, so the button read as broken.
 *
 * This module is the pure half: the explainer lines the screen renders as
 * static text, and the click runners that turn each action's REAL result into
 * a status sentence. Every dependency is injected, so the runners execute
 * under node:test without a Svelte runtime (PreflightScreen itself cannot be
 * SSR-mounted, see preflight-run-setup.test.mjs).
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 every action button has one static explainer line, "Label: what it
 *     does", and none of them relies on a hover title.
 *     [if] a button's explanation exists only as a `title` [then ⛔️] broken
 *     [if] the explainer list omits a button the screen renders [then ⛔️] broken
 *   ✔︎ 🎯 every click sets a pending status at once, then a final status built
 *     from what the action actually returned.
 *     [if] a click leaves the status line unchanged [then ⛔️] broken
 *     [if] a failed action reports success [then ⛔️] broken
 *     [if] the summary counts a non-pass check as passing [then ⛔️] broken
 */
import type { PreflightCheck } from '../api';

export type PreflightActionId = 'import' | 'recheck' | 'permissions' | 'retry-setup';

export type PreflightExplainer = { label: string; text: string };

export type PreflightSnapshot = { checks: PreflightCheck[]; error: string | null };

export const ACTION_LABELS: Record<PreflightActionId, string> = {
	import: 'Import your music',
	recheck: 'Re-check',
	permissions: 'Re-request permissions',
	'retry-setup': 'Retry setup check'
};

const EXPLAINER_TEXT: Record<PreflightActionId, string> = {
	import: 'opens setup to import rekordbox or a music folder.',
	recheck: 'runs every startup check again now instead of waiting for the next automatic check.',
	permissions:
		'attempts the protected audio read again; macOS shows its prompt here if it never has.',
	'retry-setup': 'asks the engine again whether first-run setup is needed.'
};

export const RUN_SETUP_EXPLAINER: PreflightExplainer = {
	label: 'Run setup',
	text: 'imports a library now; you can also keep using the app empty.'
};

/** The library-attached row is the only one that offers its own Run setup
 * button. Shared with PreflightCheckRow so the explainer list and the row
 * cannot disagree about when that button exists. */
export function checkOffersRunSetup(check: PreflightCheck): boolean {
	return check.id === 'library-attached' && (check.status === 'pending' || check.status === 'fail');
}

/** One explainer per rendered button, in on-screen order. `blockedReason`
 * replaces the import line's text when setup is refused, because a disabled
 * button with no visible reason is the dead-button pattern. */
export function preflightExplainers(
	visible: { actions: PreflightActionId[]; runSetup: boolean },
	blockedReason: string | null
): PreflightExplainer[] {
	const lines: PreflightExplainer[] = [];
	if (visible.runSetup) lines.push(RUN_SETUP_EXPLAINER);
	for (const id of visible.actions) {
		const text =
			id === 'import' && blockedReason !== null
				? `unavailable: ${blockedReason}`
				: EXPLAINER_TEXT[id];
		lines.push({ label: ACTION_LABELS[id], text });
	}
	return lines;
}

export const PENDING_STATUS: Record<PreflightActionId, string> = {
	import: 'Opening the importer...',
	recheck: 'Re-checking...',
	permissions: 'Asking macOS again...',
	'retry-setup': 'Retrying the setup check...'
};

function clockTime(now: Date): string {
	const pad = (n: number) => String(n).padStart(2, '0');
	return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

/** "Checked at 17:42: 3 of 5 checks pass. Still waiting on: A, B." from the
 * store state the check actually left behind. */
export function summarizePreflight(
	snapshot: PreflightSnapshot,
	now: Date,
	labelOf: (check: PreflightCheck) => string = (check) => check.label
): string {
	const at = `Checked at ${clockTime(now)}`;
	if (snapshot.error !== null) return `${at}: could not reach the engine (${snapshot.error}).`;
	const total = snapshot.checks.length;
	if (total === 0) return `${at}: the engine returned no checks yet.`;
	const waiting = snapshot.checks.filter((check) => check.status !== 'pass');
	const counts = `${at}: ${total - waiting.length} of ${total} checks pass.`;
	return waiting.length === 0
		? counts
		: `${counts} Still waiting on: ${waiting.map(labelOf).join(', ')}.`;
}

export type CheckDeps = {
	check: () => Promise<void>;
	snapshot: () => PreflightSnapshot;
	now: () => Date;
	labelOf?: (check: PreflightCheck) => string;
	setStatus: (status: string) => void;
};

export async function runRecheck(deps: CheckDeps): Promise<void> {
	deps.setStatus(PENDING_STATUS.recheck);
	await deps.check();
	deps.setStatus(summarizePreflight(deps.snapshot(), deps.now(), deps.labelOf));
}

/** The permission request IS the gated read (see `requestPermissions`), so
 * the honest report is what was re-run, where to go if macOS stayed quiet,
 * and the real result of that read. */
export async function runRequestPermissions(deps: CheckDeps): Promise<void> {
	deps.setStatus(PENDING_STATUS.permissions);
	await deps.check();
	deps.setStatus(
		'Asked macOS again by re-running the protected audio read; if nothing appeared, ' +
			'open System Settings > Privacy & Security. ' +
			summarizePreflight(deps.snapshot(), deps.now(), deps.labelOf)
	);
}

/** `runSetup` resolves to null on success and to the refusal or error
 * sentence otherwise; that value is the whole report. */
export async function runImport(
	open: () => Promise<string | null>,
	setStatus: (status: string) => void
): Promise<void> {
	setStatus(PENDING_STATUS.import);
	const failure = await open();
	setStatus(failure === null ? 'Opened the importer.' : `Could not open the importer: ${failure}`);
}

/** `retryFirstRunGate` resolves true (setup needed), false (not needed) or
 * null (failed; the gate store then holds the error). */
export async function runRetrySetupCheck(
	deps: {
		retry: () => Promise<boolean | null>;
		gateError: () => string | null;
		open: () => Promise<string | null>;
	},
	setStatus: (status: string) => void
): Promise<void> {
	setStatus(PENDING_STATUS['retry-setup']);
	const show = await deps.retry();
	if (show === true) {
		await runImport(deps.open, setStatus);
	} else if (show === false) {
		setStatus('Setup check answered: no setup needed.');
	} else {
		setStatus(`Setup check failed again: ${deps.gateError() ?? 'no error message returned'}`);
	}
}
