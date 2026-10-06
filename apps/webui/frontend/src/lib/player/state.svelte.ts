/**
 * PerformanceState: the 4 reactive rune stores, their factories, and the read
 * accessors. The only `.svelte.ts` in player/, because it is the only module
 * that owns `$state`.
 *
 * FROZEN for T4. No field is added, removed, renamed or retyped by the split;
 * modules may change who WRITES a field, never what the field is. Deck.svelte,
 * Mixer.svelte, TopBar.svelte, JogDial.svelte, PerfMeters.svelte,
 * TrackTable.svelte, performance-ipc.svelte.ts, action-glue.svelte.ts,
 * auto-play.svelte.ts and performance-hotkeys.ts all read these structures
 * directly and are untouched by the split. The shapes themselves live in
 * rb/deck-state-types.ts and rb/mixer-types.ts.
 *
 * The stores are `const` and are only ever mutated in place, never reassigned,
 * which is what makes exporting the proxies across a module boundary safe.
 *
 * The reset factories are exported (names kept) so teardown and load go through
 * exactly one definition of "empty deck" rather than inline literals.
 */

import { playbackBpm } from '$lib/rb/beat-sync-math';
import { recordDeckStateBaseline } from '$lib/rb/perf-event-log';
import { unavailableStemDeckState } from '$lib/rb/stem-graph';
import type { HotCueSlotState } from '$lib/rb/api-rb';
import type { PitchRange } from '$lib/player/constants';
import type { DeckId } from '$lib/rb/deck-slots';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import type { HeadphoneState, MixerChannelState, MixerState } from '$lib/rb/mixer-types';

/** Exported (name kept) so dispose and unload reset through one definition. */
export function _emptyDeckState(deck_id: DeckId): DeckState {
	return {
		deck_id,
		stable_id: null,
		source_path: null,
		title: null,
		artist: null,
		rating: null,
		bpm: null,
		key: null,
		key_shift_semitones: 0,
		duration_ms: null,
		position_ms: 0,
		playing: false,
		audible: false,
		transport_pending: false,
		cue_ms: null,
		pitch: 1,
		quantize_enabled: true,
		quantize_grid_beats: 1,
		beat_sync_enabled: true,
		key_sync_enabled: false,
		master_tempo_enabled: true,
		slip_enabled: false,
		slip_active: false,
		slip_position_ms: null,
		sync_mode: 'bar',
		sync_error: null,
		processor_error: null,
		stems: unavailableStemDeckState(),
		loop: null,
		safety_loop: null,
		hot_cues: [],
		hot_cue_revisions: _emptyHotCueRevisions(),
		has_rb_mapping: true,
		anlz: null,
		anlz_error: null,
		last_load_latency_ms: null,
		load_generation: 0,
		last_load_stages: null,
		is_master: false
	};
}

function _emptyHotCueRevisions(): Record<HotCueSlot, string> {
	return {
		A: '', B: '', C: '', D: '', E: '', F: '', G: '', H: ''
	};
}

/** Exported (name kept) for the hot-cue publish path still in the engine. */
export function _hotCueRevisionsFrom(slots: HotCueSlotState[]): Record<HotCueSlot, string> {
	const revisions = _emptyHotCueRevisions();
	for (const slot of slots) revisions[slot.slot] = slot.revision;
	return revisions;
}

/** Exported (name kept) so dispose resets the mixer through one definition. */
export function _defaultChannel(deck_id: DeckId): MixerChannelState {
	return {
		deck_id,
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		filter: 0.5,
		fader: 1,
		// Screenshot assign-matrix default: odd decks -> bus A, even -> bus B.
		assign: deck_id % 2 === 1 ? 'A' : 'B',
		cue_enabled: false,
		stem_eq_mode: false
	};
}

import { loadMixerConfig } from '$lib/player/mixer-config';
import { ioDeviceAccessNotChecked } from '$lib/player/io-device-access';

/** Exported (name kept) so dispose resets headphones through one definition. */
export function _defaultHeadphones(): HeadphoneState {
	const persisted = loadMixerConfig();
	const last = persisted.last_calibration;
	return {
		mix: 0,
		level: 0.5,
		selected_output_device_id: null,
		selected_master_output_device_id: null,
		selected_input_device_id: null,
		output_mode: 'practice',
		split_reason: null,
		head_delay_ms: persisted.head_delay_ms,
		alignment_mode: persisted.alignment_mode,
		master_delay_ms: persisted.master_delay_ms,
		calibration: {
			step: 'idle',
			cue_latency_ms: last === null ? null : last.cue_latency_ms,
			master_latency_ms: last === null ? null : last.master_latency_ms,
			offset_ms: last === null ? null : last.cue_latency_ms - last.master_latency_ms,
			verify_residual_ms: null,
			probe: null,
			error: null,
			diagnostics: {
				probe: 'chirp',
				alternate_probe: 'unavailable',
				failure: null,
				master_measurements_ms: [],
				cue_measurements_ms: [],
				spread_ms: null
			}
		},
		signals: {
			master: { state: 'unavailable', rms: null, peak: null, measured_at: null, source: 'application_bus', physical_output_proven: false },
			cue: { state: 'unavailable', rms: null, peak: null, measured_at: null, source: 'application_bus', physical_output_proven: false },
			input: { state: 'inactive', rms: null, peak: null, measured_at: null, source: 'captured_input', physical_output_proven: false }
		},
		routes: {
			master: { state: 'default', selected: false },
			cue: { state: 'default', selected: false }
		},
		outputs: [],
		inputs: [],
		device_access: ioDeviceAccessNotChecked(),
		supported: false,
		active: false,
		error: null
	};
}

/** Per-deck reactive UI state, keyed 1-4. Deep-reactive $state proxy. */
export const deckStates: Record<DeckId, DeckState> = $state({
	1: _emptyDeckState(1),
	2: _emptyDeckState(2),
	3: _emptyDeckState(3),
	4: _emptyDeckState(4)
});

// The four decks above are created empty on EVERY page load - a constant, not
// an event. Recording it as four ring rows restated the fact each load and
// evicted real diagnostics from the shared quiet budget, and a single unrelated
// row could then evict a deck's only state row and blind the resource probe.
// Record it once as a durable non-ring baseline instead.
recordDeckStateBaseline();

/** Explicit audio-load error per deck (backend code or decode message);
 * null = no failed load. DeckState has no audio-error field by contract,
 * so the failure state lives here, never swallowed. */
export const deckLoadErrors: Record<DeckId, string | null> = $state({
	1: null,
	2: null,
	3: null,
	4: null
});

/** Selected pitch range per deck (jog dial readout: +-8 / +-16 / WIDE). */
export const pitchRanges: Record<DeckId, PitchRange> = $state({
	1: 16,
	2: 16,
	3: 16,
	4: 16
});

/** Whole mixer surface (channel order on screen: 3 1 2 4). */
const MIXER_STATE_SINGLETON_KEY = '__mdtMixerStateSingleton';

function _createMixerState(): MixerState {
	const mixer = $state({
		channels: {
			1: _defaultChannel(1),
			2: _defaultChannel(2),
			3: _defaultChannel(3),
			4: _defaultChannel(4)
		},
		crossfader: 0.5,
		master: 1,
		headphones: _defaultHeadphones()
	});
	return mixer;
}

function _sharedMixerState(): MixerState {
	const globalRef = globalThis as typeof globalThis & {
		[MIXER_STATE_SINGLETON_KEY]?: MixerState;
	};
	if (globalRef[MIXER_STATE_SINGLETON_KEY] === undefined) {
		globalRef[MIXER_STATE_SINGLETON_KEY] = _createMixerState();
	}
	return globalRef[MIXER_STATE_SINGLETON_KEY];
}

export const mixerState: MixerState = _sharedMixerState();

let _masterWriteRevision = 0;

/** Monotonic token for distinguishing a stale teardown mute from a later write. */
export function readMasterWriteRevision(): number {
	return _masterWriteRevision;
}

/** Called by the one engine-owned master setter before it publishes a value. */
export function recordMasterWrite(): void {
	_masterWriteRevision += 1;
}

/** Per-deck store accessor (contract: singleton engine + accessor). */
export function getDeckState(deck: DeckId): DeckState {
	return deckStates[deck];
}

/** Pitch-adjusted playback BPM for jog / IPC; null until a tempo base exists.
 * Uses local PQTZ BPM (Beat Sync truth), not rekordbox tag BPM. Reactive
 * when read inside $derived. */
export function deckEffectiveBpm(deck: DeckId): number | null {
	const st = deckStates[deck];
	return playbackBpm({
		beats: st.anlz?.beatgrid.beats,
		positionSec: Math.max(0, st.position_ms / 1000),
		tempoRatio: st.pitch,
		tagBpm: st.bpm
	});
}

/** Remaining track time in ms (for the -MM:SS.d readout); null until a
 * track is loaded. */
export function deckRemainingMs(deck: DeckId): number | null {
	const st = deckStates[deck];
	return st.duration_ms === null ? null : Math.max(0, st.duration_ms - st.position_ms);
}
