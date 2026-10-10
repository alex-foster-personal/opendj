/**
 * The opt-in switch for the frame probe (PERFPROBE-01), split from
 * `frame-probe.ts` so the boot path can check it without pulling the probe
 * itself into the first-load bundle: the probe module loads only when asked for.
 */

export const FRAME_PROBE_QUERY = 'perfprobe';
export const FRAME_PROBE_STORAGE_KEY = 'mdt.perfProbe';

interface StorageLike {
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
