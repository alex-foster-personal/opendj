/**
 * USB volume import via the existing setup folder-import job.
 * Files stay on the stick. Analysis (BPM, key, beatgrid) is asked for once
 * that job has written the stick's tracks into the library, because the
 * drain only sees tracks that already have a row. The panel ranks the
 * stick's tracks around whatever deck is playing; it does not reorder the
 * drain, which runs in library order.
 */
import { getSetupJob, startFolderImport, type SetupJob } from '$lib/setup/setup-api';
import { pushToast } from '$lib/stores.svelte';
import { startAnalysisQueueDrain } from './api-ingest';
import { RbApiError } from './api-rb-error';
import { readApiErrorStatus } from '$lib/api/errors';
import { usbAccessBlocked, usbTracker, type UsbVolumeKnown } from './usb-tracker.svelte';

export const USB_IMPORT_TITLE =
	'Open this stick and analyze BPM, key and beatgrid once its tracks are in the library. Files stay on the stick. The plan shows which tracks fit what is playing.';

/** How often the folder-import job is read while analysis waits on it. */
export const IMPORT_POLL_MS = 1000;
/** How long the folder import runs before a toast says it is still going. */
export const IMPORT_WAIT_MS = 30 * 60 * 1000;

/** How often a drain refused because another one is running is asked again,
 * for as long as the page is open; a reload resumes the ask (Sol P1, PR #6223). */
export const DRAIN_RETRY_MS = 2000;

/** Mirrors TERMINAL_STATUSES in apps/engine_core/jobs/store.py. */
const TERMINAL_IMPORT_STATUSES: readonly string[] = ['succeeded', 'failed', 'cancelled', 'unknown'];

/** The folder-import jobs whose analysis is still owed, so a reload resumes their waits. */
export const PENDING_IMPORT_KEY = 'odj.usb.pendingImportJob';

function readPendingImports(): string[] {
	const raw = localStorage.getItem(PENDING_IMPORT_KEY);
	if (raw === null) return [];
	const parsed: unknown = JSON.parse(raw);
	if (!Array.isArray(parsed) || !parsed.every((id) => typeof id === 'string')) {
		throw new Error(`${PENDING_IMPORT_KEY} holds ${raw}, not a list of job ids`);
	}
	return parsed;
}

function writePendingImports(ids: readonly string[]): void {
	if (ids.length === 0) localStorage.removeItem(PENDING_IMPORT_KEY);
	else localStorage.setItem(PENDING_IMPORT_KEY, JSON.stringify(ids));
}

/** Throws when the job cannot be stored, so Open says a reload would lose the wait (Sol P1, PR #6223). */
export function rememberPendingImport(jobId: string): void {
	const ids = readPendingImports();
	if (!ids.includes(jobId)) writePendingImports([...ids, jobId]);
}

/** A job that cannot be cleared would resume on every reload and ask for a
 * drain it no longer owes, so the failure is said with a Retry (Sol P1, PR #6223). */
export function forgetPendingImport(jobId: string): void {
	try {
		writePendingImports(readPendingImports().filter((id) => id !== jobId));
	} catch (err) {
		console.error('[usb] could not clear the remembered import job:', err);
		pushToast(
			`A finished stick import could not be cleared (${errorText(err)}), so a reload would ask for its analysis again.`,
			'error',
			undefined,
			err,
			{},
			`usb-forget-${jobId}`,
			{ label: 'Retry', handler: () => forgetPendingImport(jobId) }
		);
	}
}

/**
 * On boot, resume the analysis wait for every import a reload interrupted
 * (Sol P1, PR #6223). The jobs live in the daemon; only the waiters were lost.
 */
export async function resumePendingUsbAnalysis(): Promise<void> {
	let ids: string[];
	try {
		ids = readPendingImports();
	} catch (err) {
		pushToast(`Stick imports from before the reload could not be read back, so their analysis did not resume (${errorText(err)}).`, 'error');
		return;
	}
	await Promise.all(ids.map((id) => resumeOne(id)));
}

async function resumeOne(jobId: string): Promise<void> {
	let job: SetupJob;
	try {
		job = await getSetupJob(jobId);
	} catch (err) {
		// A job the daemon no longer has can never be analyzed; anything else
		// may be transient, so the next load tries again.
		if (readApiErrorStatus(err) === 404) forgetPendingImport(jobId);
		pushToast(`A stick import from before the reload could not be read, so its analysis did not resume (${errorText(err)}).`, 'error');
		return;
	}
	await analyzeWhenImported(job);
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Read the folder-import job until it ends, however long that takes, so
 * the analysis it owes is never dropped while the page is open (Sol P1, PR
 * #6223). `onSlow` runs once if it is still going after IMPORT_WAIT_MS. */
export async function waitForImportJob(job: SetupJob, onSlow: () => void = () => {}): Promise<SetupJob> {
	const slowAt = Date.now() + IMPORT_WAIT_MS;
	let warned = false;
	let current = job;
	while (!TERMINAL_IMPORT_STATUSES.includes(current.status)) {
		if (!warned && Date.now() >= slowAt) {
			warned = true;
			onSlow();
		}
		await sleep(IMPORT_POLL_MS);
		current = await getSetupJob(job.id);
	}
	return current;
}

/**
 * Start the analysis drain only after the folder import has finished, so the
 * stick's new tracks are in the backlog the drain reads. Says the outcome in
 * one toast either way.
 */
export async function analyzeWhenImported(job: SetupJob): Promise<void> {
	// The job stays remembered unless analysis started or can never start,
	// so a failed read or a timeout is resumed on the next load (Sol P1, PR #6223).
	if ((await analyzeAfterImport(job)) === 'settled') forgetPendingImport(job.id);
}

/** 'settled': analysis started, or the import ended so it never can. 'owed':
 * a read or drain failed, and the next load tries again. */
async function analyzeAfterImport(job: SetupJob): Promise<'settled' | 'owed'> {
	let done: SetupJob;
	try {
		done = await waitForImportJob(job, () =>
			pushToast('The stick is still importing after 30 minutes. Analysis starts when it finishes.', 'info')
		);
	} catch (err) {
		pushToast(`Analysis did not start: the import could not be read (${errorText(err)}).`, 'error');
		return 'owed';
	}
	if (done.status !== 'succeeded') {
		const why = done.error ? `: ${done.error}` : '';
		pushToast(`The stick's import ended ${done.status}${why}. Analysis did not start.`, 'error');
		return 'settled';
	}
	try {
		if ((await startAnalysisQueueDrain()) !== null) {
			pushToast('Stick imported. Analysis started.', 'info');
			return 'settled';
		}
		// A drain already running read its targets when it started, maybe
		// before this import wrote the stick's rows, so start another after it.
		pushToast('Stick imported. Analysis starts when the one already running ends.', 'info');
		await startDrainAfterActive();
		pushToast("Analysis of the stick's tracks started.", 'info');
		return 'settled';
	} catch (err) {
		pushToast(`Stick imported. Analysis did not start: ${errorText(err)}.`, 'error');
		return 'owed';
	}
}

/** Ask for a drain until one starts. No deadline: the stick's analysis is
 * owed until a drain runs, however long the one ahead of it takes. */
async function startDrainAfterActive(): Promise<void> {
	for (;;) {
		await sleep(DRAIN_RETRY_MS);
		if ((await startAnalysisQueueDrain()) !== null) return;
	}
}

/** A blocked drive (USBPLAY-02) cannot be read, so its import would only
 * queue a job that fails on the first directory it lists. */
export function canImportUsbVolume(vol: UsbVolumeKnown): boolean {
	return Boolean(
		vol.mount_path &&
			vol.present &&
			!vol.simulated &&
			!vol.forgotten &&
			vol.is_music !== false &&
			!usbAccessBlocked(vol)
	);
}

function errorText(err: unknown): string {
	if (err instanceof RbApiError) return err.message;
	if (err instanceof Error) return err.message;
	return String(err);
}

export async function importUsbVolume(vol: UsbVolumeKnown): Promise<boolean> {
	if (!canImportUsbVolume(vol) || !vol.mount_path) return false;
	let job: SetupJob;
	try {
		job = await startFolderImport({ folders: [vol.mount_path] });
	} catch (err) {
		pushToast(`Could not open this stick: ${errorText(err)}`, 'error');
		return false;
	}
	try {
		rememberPendingImport(job.id);
		pushToast('Opened. Analysis starts when the stick is imported.', 'info');
	} catch (err) {
		pushToast(`Opened, but the job could not be saved (${errorText(err)}), so a reload before the import ends will not start its analysis.`, 'error');
	}
	void analyzeWhenImported(job);

	// The panel has to be open for the plan to be visible. Do not force the
	// first-seen prompt: this click already chose Open.
	usbTracker.panelOpen = true;
	try {
		const { openUsbImportPlan } = await import('./usb-import-open');
		await openUsbImportPlan(vol);
	} catch (err) {
		pushToast(`The stick is queued, but the analysis plan did not open: ${errorText(err)}`, 'error');
	}
	return true;
}
