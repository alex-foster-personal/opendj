/**
 * The scan status poller behind the Title fixes tab.
 *
 * A request that resolves after the panel was destroyed must neither update the dead component nor schedule another
 * tick: `stop()` flips a flag every await checks, because a timer that is already null cannot be cancelled by
 * clearing it. Without that, each visit to the tab during a running scan leaves one more orphan loop polling.
 */

/** Longest wait between retries after consecutive failed polls; a success returns to `pollMs`. */
export const MAX_BACKOFF_MS = 30_000;

export interface ScanPollerDeps<Scan extends { state: string }> {
	pollMs: number;
	fetchScan: () => Promise<Scan>;
	onScan: (scan: Scan) => void;
	onSettled: (scan: Scan) => Promise<void>;
	onError: (error: unknown) => void;
}

export interface ScanPoller {
	schedule: (delayMs?: number) => void;
	stop: () => void;
	isStopped: () => boolean;
}

/**
 * One awaited loading step that is dropped if the panel was destroyed while its request was in flight: the result is
 * neither applied to the dead component nor followed by the next request. True when it was applied.
 */
export async function applyWhileMounted<T>(
	isStopped: () => boolean,
	load: () => Promise<T>,
	apply: (value: T) => void
): Promise<boolean> {
	const value = await load();
	if (isStopped()) return false;
	apply(value);
	return true;
}

export function createScanPoller<Scan extends { state: string }>(
	deps: ScanPollerDeps<Scan>
): ScanPoller {
	let timer: ReturnType<typeof setTimeout> | null = null;
	let stopped = false;
	let failures = 0;

	function clear(): void {
		if (timer !== null) clearTimeout(timer);
		timer = null;
	}

	async function tick(): Promise<void> {
		timer = null;
		if (stopped) return;
		let scan: Scan;
		try {
			scan = await deps.fetchScan();
		} catch (error) {
			if (stopped) return;
			failures += 1;
			deps.onError(error);
			// The scan keeps running in the backend through a network blip, restart or 5xx: keep polling, slower.
			schedule(Math.min(deps.pollMs * 2 ** failures, MAX_BACKOFF_MS));
			return;
		}
		if (stopped) return;
		failures = 0;
		deps.onScan(scan);
		if (scan.state === 'running') {
			schedule();
			return;
		}
		await deps.onSettled(scan);
	}

	function schedule(delayMs: number = deps.pollMs): void {
		if (stopped) return;
		clear();
		timer = setTimeout(() => void tick(), delayMs);
	}

	function stop(): void {
		stopped = true;
		clear();
	}

	return { schedule, stop, isStopped: () => stopped };
}
