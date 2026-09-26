/**
 * BeatSyncMax waveform overlays for WaveRow.svelte (issue #3991).
 * Pure helpers kept out of the component for the file-size ratchet.
 */
import type { AnlzBeat, AnlzData } from '$lib/rb/anlz-types';
import { followerSyncPlayheadTone } from './wave-math';
import type { AnlzEntry } from './anlz-cache.svelte';
import type { MasterDownbeatOverlay, PlayheadTone } from './wave-playhead-render';

export function syncPlayheadToneForDeck(input: {
	audible: boolean;
	isMaster: boolean;
	stable_id: string | null;
	beat_sync_enabled: boolean;
	sync_error: string | null;
	sync_mode: 'beat' | 'bar';
	position_ms: number;
	paintAnlz: AnlzData | null;
	masterBeats: readonly AnlzBeat[] | null;
	masterState: { position_ms: number } | null;
}): PlayheadTone {
	if (!input.audible) return 'stopped';
	if (input.isMaster && input.stable_id !== null) {
		return input.beat_sync_enabled ? 'masterSynced' : 'master';
	}
	const followerBeats = input.paintAnlz?.beatgrid.beats;
	if (followerBeats === undefined || input.masterBeats === null || input.masterState === null) {
		return 'now';
	}
	const tone = followerSyncPlayheadTone({
		beatSyncEnabled: input.beat_sync_enabled,
		isMaster: input.isMaster,
		syncError: input.sync_error,
		syncMode: input.sync_mode,
		followerBeats,
		masterBeats: input.masterBeats,
		followerPosMs: input.position_ms,
		masterPosMs: input.masterState.position_ms
	});
	return tone ?? 'now';
}

export function ghostSeekBlinkVisible(nowMs: number): boolean {
	return Math.floor(nowMs / 120) % 2 === 0;
}

export function masterBeatsForDeck(
	masterState: { stable_id: string | null; anlz: AnlzData | null } | null,
	getAnlzEntry: (stable_id: string) => AnlzEntry | undefined
): readonly AnlzBeat[] | null {
	if (masterState === null || masterState.stable_id === null) return null;
	if (masterState.anlz !== null) return masterState.anlz.beatgrid.beats;
	const entry = getAnlzEntry(masterState.stable_id);
	return entry !== undefined && entry.status === 'ready' ? entry.data.beatgrid.beats : null;
}

export function masterDownbeatOverlayForDeck(input: {
	beatSyncMax: boolean;
	masterState: { anlz: AnlzData | null; position_ms: number; pitch: number } | null;
	masterBeats: readonly AnlzBeat[] | null;
	deckPositionMs: number;
	deckPitch: number;
	hasTrustedBeatGrid: (anlz: AnlzData) => boolean;
}): MasterDownbeatOverlay | null {
	if (!input.beatSyncMax || input.masterState === null || input.masterBeats === null) return null;
	if (input.masterState.anlz === null || !input.hasTrustedBeatGrid(input.masterState.anlz)) return null;
	return {
		masterBeats: input.masterBeats,
		masterPositionSec: input.masterState.position_ms / 1000,
		followerPositionSec: input.deckPositionMs / 1000,
		masterPitch: input.masterState.pitch,
		followerPitch: input.deckPitch
	};
}
