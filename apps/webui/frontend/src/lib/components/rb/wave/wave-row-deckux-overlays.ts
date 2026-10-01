/**
 * BeatSyncMax waveform overlays for WaveRow.svelte (issue #3991).
 * Pure helpers kept out of the component for the file-size ratchet.
 */
import type { AnlzData } from '$lib/rb/anlz-types';
import type { AnlzEntry } from './anlz-cache.svelte';
import type { MasterDownbeatOverlay } from './wave-playhead-render';

const GHOST_SEEK_BLINK_HALF_PERIOD_MS = 120;

export function ghostSeekBlinkVisible(nowMs: number): boolean {
	return Math.floor(nowMs / GHOST_SEEK_BLINK_HALF_PERIOD_MS) % 2 === 0;
}

/** Ghost-cursor inputs for ONE painted frame (pin 25d6dea6). The ghost is
 * drawn on the waveform canvas and scrolls with it, so its blink is clocked by
 * the frame that paints it, never by reactive state: `performance.now()` is
 * not a reactive dependency, and a `$derived` over it freezes at mount.
 * `blinkPhase` is the repaint key: null while disarmed so an idle deck is not
 * repainted on a blink clock. */
export function ghostSeekFrame(
	armed: { target_position_ms: number } | null,
	nowMs: number
): { ghostSeekMs: number | null; ghostSeekVisible: boolean; blinkPhase: number | null } {
	if (armed === null) return { ghostSeekMs: null, ghostSeekVisible: false, blinkPhase: null };
	return {
		ghostSeekMs: armed.target_position_ms,
		ghostSeekVisible: ghostSeekBlinkVisible(nowMs),
		blinkPhase: Math.floor(nowMs / GHOST_SEEK_BLINK_HALF_PERIOD_MS)
	};
}

/** The master's analysis from the one source that will be painted: the deck's
 * own published anlz, else its ready shared-cache entry. Returned whole so the
 * overlay's trust check validates the SAME AnlzData whose beats it draws. */
export function masterAnlzForDeck(
	masterState: { stable_id: string | null; anlz: AnlzData | null } | null,
	getAnlzEntry: (stable_id: string) => AnlzEntry | undefined
): AnlzData | null {
	if (masterState === null || masterState.stable_id === null) return null;
	if (masterState.anlz !== null) return masterState.anlz;
	const entry = getAnlzEntry(masterState.stable_id);
	return entry !== undefined && entry.status === 'ready' ? entry.data : null;
}

export function masterDownbeatOverlayForDeck(input: {
	beatSyncMax: boolean;
	masterState: { position_ms: number; pitch: number } | null;
	masterAnlz: AnlzData | null;
	deckPositionMs: number;
	deckPitch: number;
	hasTrustedBeatGrid: (anlz: AnlzData) => boolean;
}): MasterDownbeatOverlay | null {
	if (!input.beatSyncMax || input.masterState === null || input.masterAnlz === null) return null;
	if (!input.hasTrustedBeatGrid(input.masterAnlz)) return null;
	return {
		masterBeats: input.masterAnlz.beatgrid.beats,
		masterPositionSec: input.masterState.position_ms / 1000,
		followerPositionSec: input.deckPositionMs / 1000,
		masterPitch: input.masterState.pitch,
		followerPitch: input.deckPitch
	};
}
