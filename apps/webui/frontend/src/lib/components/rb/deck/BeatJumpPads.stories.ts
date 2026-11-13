import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemDeckState } from '$lib/rb/stem-types';

import BeatJumpPads from './BeatJumpPads.svelte';

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

const BPM = 128;
/** The invented track's title; a constant so no `title:` literal sits above the meta. */
const TRACK_TITLE = 'Night Drive (Extended Mix)';
const DURATION_MS = 240_000;
const beatCount = Math.floor((DURATION_MS / 1000) * (BPM / 60));
const beats = Array.from({ length: beatCount }, (_, i) => ({
	n: (i % 4) + 1,
	bpm: BPM,
	t: (i * 60) / BPM
}));

const deckState = (over: Partial<DeckState>): DeckState => ({
	deck_id: 1,
	stable_id: 'a'.repeat(40),
	edit_id: null,
	edit_in_ms: null,
	edit_out_ms: null,
	source_path: '/music/Lumen Fields - Night Drive (Extended Mix).flac',
	title: TRACK_TITLE,
	artist: 'Lumen Fields',
	rating: 4,
	bpm: BPM,
	key: '8A',
	key_shift_semitones: 0,
	duration_ms: DURATION_MS,
	position_ms: 60_000,
	playing: false,
	audible: false,
	transport_pending: false,
	cue_ms: null,
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
	pending_loop_in_ms: null,
	last_loop: null,
	hot_cues: [],
	hot_cue_revisions: { A: '', B: '', C: '', D: '', E: '', F: '', G: '', H: '' },
	has_rb_mapping: true,
	anlz: {
		stable_id: 'a'.repeat(40),
		points: 1200,
		waveform: {} as never,
		beatgrid: { source: 'rekordbox', beat_count: beats.length, beats },
		cues: [],
		phrases: []
	} as never,
	anlz_error: null,
	last_load_latency_ms: 850,
	load_generation: 1,
	last_load_stages: null,
	last_load_stage_sources: null,
	is_master: false,
	...over
});

const meta = {
	title: 'Components/Deck/BeatJumpPads',
	component: BeatJumpPads,
	tags: ['autodocs'],
	argTypes: { pending: { control: 'boolean' } },
	args: {
		deck: deckState({}),
		pending: false,
		onJump: fn(async () => {})
	}
} satisfies Meta<typeof BeatJumpPads>;
export default meta;
type Story = StoryObj<typeof meta>;

/** Track with a beatgrid: the eight pads jump back (A-D) and forward (E-H) 1, 2, 4, 8 beats. */
export const Ready: Story = {};

/** A deck command is pending, so every pad is inert with that reason. */
export const Pending: Story = { args: { pending: true } };
