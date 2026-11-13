/**
 * Shared deck fixtures for the rb/deck Storybook stories: one idle deck with a
 * loaded track and no stems. Stories override fields (anlz included) per story.
 */
import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemDeckState } from '$lib/rb/stem-types';

const TRACK_TITLE = 'Night Drive (Extended Mix)';

const stems = (): StemDeckState => ({
	status: 'unavailable',
	source: null,
	model: null,
	layout: null,
	available_controls: [],
	alignment: null,
	controls: {
		vocal: { muted: false, solo: false, gain: 0.5, fx_send: 0 },
		instrumental: { muted: false, solo: false, gain: 0.5, fx_send: 0 },
		drums: { muted: false, solo: false, gain: 0.5, fx_send: 0 },
		bass: { muted: false, solo: false, gain: 0.5, fx_send: 0 },
		other: { muted: false, solo: false, gain: 0.5, fx_send: 0 }
	},
	error: null,
	load: null
});

export const storyDeckState = (over: Partial<DeckState> = {}): DeckState => ({
	deck_id: 1,
	stable_id: 'a'.repeat(40),
	edit_id: null,
	edit_in_ms: null,
	edit_out_ms: null,
	source_path: '/music/Lumen Fields - Night Drive (Extended Mix).flac',
	title: TRACK_TITLE,
	artist: 'Lumen Fields',
	rating: 4,
	bpm: 128,
	key: '8A',
	key_shift_semitones: 0,
	duration_ms: 240_000,
	position_ms: 60_000,
	playing: false,
	audible: false,
	transport_pending: false,
	cue_ms: 30_000,
	pitch: 1,
	quantize_enabled: true,
	quantize_grid_beats: 1,
	beat_sync_enabled: false,
	key_sync_enabled: false,
	key_sync_mode: 'exact',
	key_sync_target: null,
	master_tempo_enabled: true,
	slip_enabled: false,
	slip_active: false,
	slip_position_ms: null,
	sync_mode: 'bar',
	sync_error: null,
	processor_error: null,
	stems: stems(),
	loop: null,
	safety_loop: null,
	hot_cues: [],
	hot_cue_revisions: { A: '', B: '', C: '', D: '', E: '', F: '', G: '', H: '' },
	has_rb_mapping: true,
	anlz: null,
	anlz_error: null,
	last_load_latency_ms: 850,
	load_generation: 1,
	last_load_stages: null,
	last_load_stage_sources: null,
	is_master: false,
	pending_loop_in_ms: null,
	last_loop: null,
	...over
});
