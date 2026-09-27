/**
 * Build and validate Gig rescue snapshot payloads (RESCUE-01).
 */

import type { DeckId } from '$lib/rb/deck-id';
import { encodeBeatStamp, type RescueBeatStamp } from '$lib/rb/rescue-beat-stamp';
import type { PerformanceState } from '$lib/rb/performance-ipc.svelte';
import type { DeckLayoutMode } from '$lib/rb/deck-layout-prefs';
import type { HeadphoneOutputMode } from '$lib/rb/mixer-types';

const DECK_IDS: DeckId[] = [1, 2, 3, 4];

export const RESCUE_PLAY_WINDOW_MS = 10 * 60 * 1000;
export const RESCUE_LAYOUT_WINDOW_MS = 24 * 60 * 60 * 1000;

export type RescueSnapshotReason = 'periodic' | 'transport' | 'load';

export interface RescueDeckSnapshot {
	deck_id: DeckId;
	stable_id: string | null;
	source_path: string | null;
	playing: boolean;
	position_ms: number;
	beat_stamp: RescueBeatStamp;
	pitch: number;
	pitch_range: 8 | 16 | 100;
	master_tempo_enabled: boolean;
	key_sync_enabled: boolean;
	quantize_enabled: boolean;
	beat_sync_enabled: boolean;
	sync_mode: 'beat' | 'bar';
	is_master: boolean;
	cue_ms: number | null;
	loop: {
		in_ms: number;
		out_ms: number;
		engaged: boolean;
		beat_length: number | null;
	} | null;
	hot_cue_armed: { slot: string; target_position_ms: number } | null;
	stems: {
		vocal: { muted: boolean; solo: boolean; gain: number };
		instrumental: { muted: boolean; solo: boolean; gain: number };
		drums: { muted: boolean; solo: boolean; gain: number };
	};
	mixer_channel: {
		trim: number;
		eq_high: number;
		eq_mid: number;
		eq_low: number;
		filter: number;
		fader: number;
		assign: 'A' | 'B' | 'THRU';
		cue_enabled: boolean;
	};
}

export interface RescueSnapshot {
	schema: 1;
	captured_at_ms: number;
	reason: RescueSnapshotReason;
	app_posture: 'gig';
	master_deck: DeckId | null;
	playlist_id: string | null;
	deck_layout?: DeckLayoutMode;
	decks: Record<DeckId, RescueDeckSnapshot>;
	mixer: {
		crossfader: number;
		master: number;
		headphones: {
			mix: number;
			level: number;
			output_mode: HeadphoneOutputMode;
			selected_master_output_device_id: string | null;
			selected_output_device_id: string | null;
			/** RESCUE-05: the enumerated label of each selected sink at capture, the
			 * half of the descriptor that survives the per-origin id salt (see
			 * `$lib/player/output-device-resolve`). Null when nothing is selected or
			 * the label was hidden; ABSENT on snapshots written before RESCUE-05. */
			selected_master_output_device_label?: string | null;
			selected_output_device_label?: string | null;
		};
	};
}

/** The label to persist beside a selected sink id: null when nothing is
 * selected, the id is not enumerated, or the browser hid the label. */
function _selectedOutputLabel(
	outputs: readonly { id: string; label: string }[],
	deviceId: string | null
): string | null {
	if (deviceId === null) return null;
	const label = outputs.find((output) => output.id === deviceId)?.label ?? '';
	return label.trim() === '' ? null : label;
}

export function buildRescueSnapshot(
	state: PerformanceState,
	reason: RescueSnapshotReason,
	captured_at_ms: number,
	sourcePaths: Readonly<Record<DeckId, string | null>>,
	deck_layout: DeckLayoutMode = 'more'
): RescueSnapshot {
	const decks = {} as Record<DeckId, RescueDeckSnapshot>;
	for (const deckId of DECK_IDS) {
		const deck = state.decks[deckId];
		const channel = state.mixer.channels[deckId];
		const beats =
			deck.beatgrid.map((beat) => ({ n: beat.n, t: beat.time_ms / 1000 })) ?? [];
		const armed = deck.hot_cue_armed;
		decks[deckId] = {
			deck_id: deckId,
			stable_id: deck.stable_id,
			source_path: sourcePaths[deckId] ?? null,
			playing: deck.playing,
			position_ms: deck.position_ms,
			beat_stamp: encodeBeatStamp(beats, deck.position_ms),
			pitch: deck.pitch,
			pitch_range: deck.pitch_range,
			master_tempo_enabled: deck.master_tempo_enabled,
			key_sync_enabled: deck.key_sync_enabled,
			quantize_enabled: deck.quantize_enabled,
			beat_sync_enabled: deck.beat_sync_enabled,
			sync_mode: deck.sync_mode,
			is_master: deck.is_master,
			cue_ms: deck.cue_ms,
			loop: deck.loop === null ? null : { ...deck.loop },
			hot_cue_armed:
				armed === null
					? null
					: { slot: armed.slot, target_position_ms: armed.target_position_ms },
			stems: {
				vocal: { ...deck.stems.controls.vocal },
				instrumental: { ...deck.stems.controls.instrumental },
				drums: { ...deck.stems.controls.drums }
			},
			mixer_channel: {
				trim: channel.trim,
				eq_high: channel.eq_high,
				eq_mid: channel.eq_mid,
				eq_low: channel.eq_low,
				filter: channel.filter,
				fader: channel.fader,
				assign: channel.assign,
				cue_enabled: channel.cue_enabled
			}
		};
	}
	return {
		schema: 1,
		captured_at_ms,
		reason,
		app_posture: 'gig',
		master_deck: state.master_deck,
		playlist_id: state.browser.active_playlist,
		deck_layout,
		decks,
		mixer: {
			crossfader: state.mixer.crossfader,
			master: state.mixer.master,
			headphones: {
				mix: state.mixer.headphones.mix,
				level: state.mixer.headphones.level,
				output_mode: state.mixer.headphones.output_mode,
				selected_master_output_device_id:
					state.mixer.headphones.selected_master_output_device_id,
				selected_output_device_id: state.mixer.headphones.selected_output_device_id,
				selected_master_output_device_label: _selectedOutputLabel(
					state.mixer.headphones.outputs,
					state.mixer.headphones.selected_master_output_device_id
				),
				selected_output_device_label: _selectedOutputLabel(
					state.mixer.headphones.outputs,
					state.mixer.headphones.selected_output_device_id
				)
			}
		}
	};
}

export function serializeRescueSnapshot(snapshot: RescueSnapshot): string {
	return JSON.stringify(snapshot);
}

export function parseRescueSnapshot(raw: string | unknown): RescueSnapshot | null {
	try {
		const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
		const snapshot = parsed as RescueSnapshot;
		if (snapshot?.schema !== 1) return null;
		if (typeof snapshot.captured_at_ms !== 'number') return null;
		if (snapshot.app_posture !== 'gig') return null;
		return snapshot;
	} catch {
		return null;
	}
}
