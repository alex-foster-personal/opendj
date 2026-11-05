import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemDeckState } from '$lib/rb/stem-types';

import TransportCluster from './TransportCluster.svelte';

/**
 * Pure props: the deck is a hand-built DeckState fixture for an invented
 * track. CUE and play/pause only call `onCue` / `onPlayPause` (recorded in
 * the Actions panel); no audio engine is involved, so pressing them changes
 * nothing on screen. Show a different state by choosing a story.
 */
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

const deckState = (over: Partial<DeckState> = {}): DeckState => ({
	deck_id: 1,
	stable_id: 'a'.repeat(40),
	source_path: '/music/Lumen Fields - Night Drive (Extended Mix).flac',
	title: 'Night Drive (Extended Mix)',
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
	is_master: false,
	...over
});

const meta = {
	title: 'Components/Deck/TransportCluster',
	component: TransportCluster,
	tags: ['autodocs'],
	argTypes: { pending: { control: 'boolean' } },
	args: {
		deck: deckState(),
		pending: false,
		quantizedLaunchArmed: null,
		onCue: fn(async () => {}),
		onPlayPause: fn(async () => {})
	}
} satisfies Meta<typeof TransportCluster>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Track loaded and paused with a cue stored: both buttons live, play glyph shown. */
export const Paused: Story = {};

/** Playing: the play button shows the pause glyph and reads as on. */
export const Playing: Story = { args: { deck: deckState({ playing: true, audible: true }) } };

/** No track loaded: both buttons disabled with the "no track loaded" title. */
export const EmptyDeck: Story = {
	args: {
		deck: deckState({
			stable_id: null,
			title: null,
			artist: null,
			bpm: null,
			key: null,
			duration_ms: null,
			position_ms: 0,
			cue_ms: null
		})
	}
};

/** Loaded with no cue set yet: CUE's title says it will set a cue at the playhead. */
export const NoCueSet: Story = { args: { deck: deckState({ cue_ms: null }) } };

/** A command is in flight: aria-busy only; the buttons are NOT disabled, so they never blink. */
export const Pending: Story = { args: { pending: true } };

/** Quantized launch armed: the play button shows a countdown to the next beat boundary. */
export const QuantizedLaunchArmed: Story = {
	args: { quantizedLaunchArmed: { remaining_ms: 1400, launch_at_context_sec: 12.5 } }
};
