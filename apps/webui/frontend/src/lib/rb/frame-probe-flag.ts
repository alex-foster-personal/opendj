/**
 * The opt-in switch for the frame probe (PERFPROBE-01), split from
 * `frame-probe.ts` so the boot path can check it without pulling the probe
 * itself into the first-load bundle: the probe module loads only when asked for.
 * The same switch arms the live handles probe (PERF-LEAK-11789).
 */

export const FRAME_PROBE_QUERY = 'perfprobe';
export const FRAME_PROBE_STORAGE_KEY = 'mdt.perfProbe';

export interface StorageLike {
	getItem(key: string): string | null;
}

/** True when the page URL or localStorage asks for the probe. */
export function frameProbeRequested(search: string, storage: StorageLike | null): boolean {
	try {
		if (new URLSearchParams(search).get(FRAME_PROBE_QUERY) === '1') return true;
	} catch {
		// A malformed query string asks for nothing.
	}
	try {
		return storage?.getItem(FRAME_PROBE_STORAGE_KEY) === '1';
	} catch {
		return false;
	}
}

interface LiveHandlesProbeModule {
	installLiveHandlesProbe: () => { uninstall(): void };
}

/**
 * Arm the live handles probe (PERF-LEAK-11789) when the switch is on. Off: the
 * probe module never loads and nothing is wrapped. Resolves to its teardown.
 */
export function armLiveHandlesProbe(
	search: string,
	storage: StorageLike | null,
	load: () => Promise<LiveHandlesProbeModule> = () => import('./live-handles-probe')
): Promise<(() => void) | null> {
	if (!frameProbeRequested(search, storage)) return Promise.resolve(null);
	return load().then(
		(mod) => {
			const probe = mod.installLiveHandlesProbe();
			return () => probe.uninstall();
		},
		() => null
	);
}
