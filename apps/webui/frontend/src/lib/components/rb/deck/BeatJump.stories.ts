import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemDeckState } from '$lib/rb/stem-types';

import { contracts } from './BeatJump.contract';
import BeatJump from './BeatJump.svelte';

const stems = (): StemDeckState => ({
	status: 'unavailable',
	source: null,
	model: null,
	layout: null,
	available_controls: [],
	alignment: null,
	controls: {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 },
		bass: { muted: false, solo: false, gain: 0.5 },
		other: { muted: false, solo: false, gain: 0.5 }
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
	is_master: false,
	...over
});

const meta = {
	title: 'Contracts/Deck/BeatJump',
	component: BeatJump,
	tags: ['autodocs'],
	argTypes: { pending: { control: 'boolean' } },
	args: { deck: deckState({}), pending: false, onJump: fn(async () => {}) }
} satisfies Meta<typeof BeatJump>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Ready: Story = contracts.Ready;
export const AtFirstBeat: Story = contracts.AtFirstBeat;
export const Pending: Story = contracts.Pending;
export const NoBeatgrid: Story = contracts.NoBeatgrid;

/** Playhead near the start: the grid still has beats on both sides, so every jump stays live (the engine clamps a jump at the edge). */
export const NearTrackStart: Story = {
	args: { deck: deckState({ position_ms: 1_500 }) },
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByRole('button', { name: 'jump -8 beats deck 1' })).toBeEnabled();
		await expect(canvas.getByRole('button', { name: 'jump +8 beats deck 1' })).toBeEnabled();
	}
};

/** Playhead near the end: the grid still has beats past the playhead, so every jump stays live (the engine clamps a jump at the edge). */
export const NearTrackEnd: Story = {
	args: { deck: deckState({ position_ms: DURATION_MS - 1_500 }) },
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByRole('button', { name: 'jump +8 beats deck 1' })).toBeEnabled();
		await expect(canvas.getByRole('button', { name: 'jump -8 beats deck 1' })).toBeEnabled();
	}
};

/** Empty deck: no stable id, so every button says no track loaded. */
export const NoTrack: Story = {
	args: {
		deck: deckState({
			stable_id: null,
			title: null,
			artist: null,
			duration_ms: null,
			anlz: null,
			position_ms: 0
		})
	},
	play: async ({ canvasElement }) => {
		const buttons = Array.from(canvasElement.querySelectorAll('button'));
		await expect(buttons.length).toBeGreaterThan(0);
		for (const b of buttons) await expect(b).toBeDisabled();
	}
};
