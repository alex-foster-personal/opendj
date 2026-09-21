/**
 * Gig vs Prep app-level resource posture (PERFMODE-03).
 * Must match apps/shared/app_posture.py numeric constants.
 */

export type AppPosture = 'prep' | 'gig';

export const PREP_LIBRARY_POLL_MS = 60_000;
export const GIG_LIBRARY_POLL_MS = 300_000;
export const GIG_PREFETCH_TRACKS = 2;
export const GIG_PREFETCH_BYTES = 24 * 1024 * 1024;
/** Gig ceiling for decoded preview audio: the LOW tier's own cap. */
export const GIG_PREVIEW_PCM_BYTES = 64 * 1024 * 1024;

let _posture: AppPosture = 'prep';

export function resolvedPosture(): AppPosture {
	return _posture;
}

export function setResolvedPosture(next: AppPosture): void {
	_posture = next;
}

export function libraryFallbackPollMs(): number {
	return _posture === 'gig' ? GIG_LIBRARY_POLL_MS : PREP_LIBRARY_POLL_MS;
}

export function shouldRunLibraryFallbackPoll(
	nowMs: number,
	lastRunMs: number,
	busOpen: boolean
): boolean {
	if (busOpen) return false;
	const interval = libraryFallbackPollMs();
	return nowMs - lastRunMs >= interval;
}

export function prefetchTrackCapForPosture(tierCap: number): number {
	if (_posture === 'gig') return Math.min(tierCap, GIG_PREFETCH_TRACKS);
	return tierCap;
}

/**
 * Gig floors the preview's decoded-audio budget to the LOW tier's.
 *
 * During a gig the machine's memory belongs to the decks. Browsing a library
 * mid-set is normal; holding a quarter of a gigabyte of previously auditioned
 * PCM while doing it is not.
 */
export function previewPcmByteCapForPosture(tierCap: number): number {
	if (_posture === 'gig') return Math.min(tierCap, GIG_PREVIEW_PCM_BYTES);
	return tierCap;
}

export function prefetchByteCapForPosture(tierCap: number): number {
	if (_posture === 'gig') return Math.min(tierCap, GIG_PREFETCH_BYTES);
	return tierCap;
}
