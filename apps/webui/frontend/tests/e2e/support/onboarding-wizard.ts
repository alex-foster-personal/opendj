/**
 * The first-run wizard, driven the way a new user drives it: by role and
 * visible label, never by store internals. Shared by the onboarding gauntlet
 * specs so each spec reads as the user's story and nothing else.
 *
 * The one thing read off the API rather than the screen is ground truth for
 * an assertion (the engine's own job row, track count or setup status), so a
 * screen that disagrees with the engine is a failure rather than a match.
 */
import { expect, type Locator, type Page } from '@playwright/test';

/** Time-to-wizard budget for a cold first open, in ms. Measured first (see
 * specs/onboarding-gauntlet.md, round 0), then set with headroom for a loaded
 * CI host. Ratchets with the experiment rounds, never silently. */
export const TIME_TO_WIZARD_BUDGET_MS = 15_000;

/** How long a small folder import may take end to end before the case reds.
 * Issue #3456 is a measured ~90 s stall before the first progress emit on CI,
 * so this is the bound that would catch it getting worse, not a target. */
export const IMPORT_BUDGET_MS = 150_000;

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'unknown']);

export function setupDialog(page: Page): Locator {
	return page.getByRole('dialog', { name: 'First-run setup' });
}

export interface JobRow {
	id: string;
	kind: string;
	status: string;
	message?: string | null;
	error?: string | null;
	progress?: number | null;
}

export interface SetupStatus {
	library_empty: boolean;
	tracks: number;
	dismissed: boolean;
	dev_mode: boolean;
	should_show_wizard: boolean;
	last_import: { kind: string; tracks_written?: number; files_seen?: number; files_rejected_unplayable?: number } | null;
}

export async function readSetupStatus(origin: string): Promise<SetupStatus> {
	const response = await fetch(`${origin}/api/v1/setup/status`);
	if (!response.ok) throw new Error(`GET /api/v1/setup/status -> ${response.status}: ${await response.text()}`);
	return (await response.json()) as SetupStatus;
}

export async function readJob(origin: string, jobId: string): Promise<JobRow> {
	const response = await fetch(`${origin}/api/v1/jobs/${jobId}`);
	if (!response.ok) throw new Error(`GET /api/v1/jobs/${jobId} -> ${response.status}: ${await response.text()}`);
	return (await response.json()) as JobRow;
}

export async function waitForTerminalJob(origin: string, jobId: string, timeoutMs = IMPORT_BUDGET_MS): Promise<JobRow> {
	const deadline = Date.now() + timeoutMs;
	let last: JobRow | null = null;
	while (Date.now() < deadline) {
		last = await readJob(origin, jobId);
		if (TERMINAL.has(last.status)) return last;
		await new Promise((resolve) => setTimeout(resolve, 250));
	}
	throw new Error(`job ${jobId} not terminal after ${timeoutMs} ms; last row ${JSON.stringify(last)}`);
}

/** Open the app as a new user does and return how long the wizard took. */
export async function landAndTimeWizard(page: Page, origin: string): Promise<number> {
	const started = Date.now();
	await page.goto(`${origin}/`);
	await expect(setupDialog(page)).toBeVisible({ timeout: TIME_TO_WIZARD_BUDGET_MS * 4 });
	return Date.now() - started;
}

export interface PackagedTriggerLanding {
	timeToWizardMs: number;
	preflightRequestsHeld: number;
	preflightResponsesBeforeWizard: number;
}

/**
 * Land with GET /api/v1/preflight held back until the wizard is up, so only
 * the packaged trigger (`runFirstRunGate()` over /setup/status) can open it.
 * The preflight empty-library fallback in +layout.svelte would otherwise open
 * it too and mask a broken packaged trigger (mutation M2, round 0). Held, not
 * mocked: every held request continues to the real engine once the wizard is
 * visible. Times out red if the packaged trigger never opens the wizard.
 */
export async function landWithOnlyThePackagedTrigger(page: Page, origin: string): Promise<PackagedTriggerLanding> {
	const isPreflight = (url: URL): boolean => url.pathname === '/api/v1/preflight';
	let release!: () => void;
	const wizardUp = new Promise<void>((resolve) => (release = resolve));
	let held = 0;
	let answered = 0;
	page.on('response', (response) => {
		if (isPreflight(new URL(response.url()))) answered += 1;
	});
	await page.route(isPreflight, async (route) => {
		held += 1;
		await wizardUp;
		await route.continue();
	});
	const started = Date.now();
	await page.goto(`${origin}/`);
	await expect(setupDialog(page)).toBeVisible({ timeout: TIME_TO_WIZARD_BUDGET_MS * 4 });
	const timeToWizardMs = Date.now() - started;
	const preflightResponsesBeforeWizard = answered;
	release();
	return { timeToWizardMs, preflightRequestsHeld: held, preflightResponsesBeforeWizard };
}

/** Welcome -> "Get started" -> the folder branch of the detect step. */
export async function chooseFolderBranch(page: Page): Promise<Locator> {
	const dialog = setupDialog(page);
	await dialog.getByRole('button', { name: 'Get started' }).click();
	await dialog.getByLabel('A folder of audio files (no rekordbox needed)').check();
	await expect(dialog.getByRole('heading', { name: 'Point at a folder' })).toBeVisible();
	return dialog;
}

/** Type a folder path and press "Check this folder"; returns the scan response body. */
export async function checkFolder(page: Page, folder: string): Promise<Record<string, unknown>> {
	const dialog = setupDialog(page);
	await dialog.getByLabel('Folder to import').fill(folder);
	const scanned = page.waitForResponse((response) => response.url().includes('/api/v1/setup/detect/folder'));
	await dialog.getByRole('button', { name: 'Check this folder' }).click();
	const response = await scanned;
	expect(response.ok(), `detect/folder answered ${response.status()}`).toBe(true);
	return (await response.json()) as Record<string, unknown>;
}

/** Press "Import this folder"; returns the job id the engine accepted. */
export async function startFolderImport(page: Page): Promise<string> {
	const dialog = setupDialog(page);
	const queued = page.waitForResponse((response) => response.url().includes('/api/v1/setup/import/folder'));
	await dialog.getByRole('button', { name: /^Import (this folder|these folders)$/ }).click();
	const response = await queued;
	expect(response.status(), `import/folder answered ${response.status()}: ${await response.text()}`).toBe(202);
	const body = (await response.json()) as { id: string };
	await expect(dialog.getByRole('heading', { name: 'Importing' })).toBeVisible();
	return body.id;
}

/** From the progress step to the Done step, declining stems on the way. */
export async function continueToDone(page: Page): Promise<Locator> {
	const dialog = setupDialog(page);
	await expect(dialog.getByText('Import finished', { exact: false }).first()).toBeVisible({ timeout: IMPORT_BUDGET_MS });
	await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
	await expect(dialog.getByRole('heading', { name: 'Stems analysis' })).toBeVisible();
	await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
	await expect(dialog.getByRole('heading', { name: 'Done' })).toBeVisible();
	return dialog;
}

/** Shrink the wizard to its chip, the user's way to the app while it is open. */
export async function minimiseWizard(page: Page): Promise<void> {
	await setupDialog(page).getByRole('button', { name: 'Minimise', exact: true }).click();
	await expect(setupDialog(page)).toHaveCount(0);
}

/** The library rows the user can see in All Tracks. */
export async function allTracksRowCount(page: Page, expected: number): Promise<void> {
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator('[data-testid="track-row"]')).toHaveCount(expected, { timeout: 30_000 });
}

/** Every escape the folder step must keep offering, whatever the scan said. */
export async function expectFolderEscapesEnabled(page: Page): Promise<void> {
	const dialog = setupDialog(page);
	for (const name of ['Back', 'Look for rekordbox instead', 'Continue without importing']) {
		await expect(dialog.getByRole('button', { name, exact: true })).toBeEnabled();
	}
}
