import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, userEvent, within } from 'storybook/test';

import type { AnlzData } from '$lib/rb/anlz-types';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemDeckState } from '$lib/rb/stem-types';

import LoopSourceMenu from './LoopSourceMenu.svelte';

const TRACK_TITLE = 'Night Drive (Extended Mix)';

/**
 * Pure props: opens from the trigger, so stories click it to show the menu.
 * Stored loops come from deck.anlz.cues; callbacks are recorded in Actions.
 */
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

const loopAnlz = (): AnlzData =>
	({
		stable_id: 'a'.repeat(40),
		points: 1200,
		waveform: {} as never,
		beatgrid: { source: 'rekordbox', beat_count: 8, beats: [] },
		cues: [
			{
				kind: 'hot_cue',
				slot: 'A',
				in_ms: 64_000,
				out_ms: 71_500,
				is_loop: true,
				active_loop: false,
				beat_loop_size: null,
				color_table_index: null,
				comment: 'build'
			},
			{
				kind: 'memory',
				slot: null,
				in_ms: 96_000,
				out_ms: 103_500,
				is_loop: true,
				active_loop: false,
				beat_loop_size: null,
				color_table_index: null,
				comment: null
			}
		],
		phrases: []
	}) as unknown as AnlzData;

const deckState = (over: Partial<DeckState> = {}): DeckState => ({
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
	anlz: loopAnlz(),
	anlz_error: null,
	last_load_latency_ms: 850,
	load_generation: 1,
	last_load_stages: null,
	is_master: false,
	pending_loop_in_ms: null,
	last_loop: null,
	...over
});

const meta = {
	title: 'Components/Deck/LoopSourceMenu',
	component: LoopSourceMenu,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	args: {
		deck: deckState(),
		deckId: 1,
		pending: false,
		onStoredLoop: fn(async () => true),
		onDisengage: fn(async () => true),
		onSetActive: fn(async () => true)
	}
} satisfies Meta<typeof LoopSourceMenu>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Closed: Story = {
	play: async ({ canvasElement }) => {
		const trigger = canvasElement.querySelector('[data-testid="loop-source-deck-1"]');
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(trigger).toHaveTextContent('INT');
		await expect(canvasElement.querySelector('[role="menu"]')).toBeNull();
	}
};

export const Open: Story = {
	args: {
		deck: deckState({
			loop: { in_ms: 64_000, out_ms: 71_500, engaged: true, beat_length: null }
		})
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		const trigger = canvas.getByTestId('loop-source-deck-1');
		await expect(trigger).toHaveTextContent('HOT A');
		await userEvent.click(trigger);
		await expect(trigger).toHaveAttribute('aria-expanded', 'true');
		const items = canvas.getAllByRole('menuitemradio');
		await expect(items).toHaveLength(3);
		await expect(canvas.getByTestId('loop-source-hot-A-64000-deck-1')).toHaveAttribute(
			'aria-checked',
			'true'
		);
		await expect(canvas.getByTestId('loop-source-int-deck-1')).toHaveAttribute(
			'aria-checked',
			'false'
		);
		await expect(canvas.getByTestId('loop-source-mem-96000-103500-deck-1')).toHaveAttribute(
			'aria-checked',
			'false'
		);
	}
};

export const PickStoredLoop: Story = {
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		await userEvent.click(canvas.getByTestId('loop-source-deck-1'));
		await userEvent.click(canvas.getByTestId('loop-source-mem-96000-103500-deck-1'));
		await expect(args.onStoredLoop).toHaveBeenCalledTimes(1);
		await expect(args.onStoredLoop).toHaveBeenCalledWith({ in_ms: 96_000, out_ms: 103_500 });
		await expect(canvas.getByTestId('loop-source-deck-1')).toHaveAttribute(
			'aria-expanded',
			'false'
		);
	}
};

export const NoStoredLoops: Story = {
	args: {
		deck: deckState({
			anlz: { ...loopAnlz(), cues: [] } as unknown as AnlzData
		})
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await userEvent.click(canvas.getByTestId('loop-source-deck-1'));
		const items = canvas.getAllByRole('menuitemradio');
		await expect(items).toHaveLength(1);
		await expect(canvasElement).toHaveTextContent('No stored loops on this track');
	}
};

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
			anlz: null
		})
	},
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="loop-source-deck-1"]')).toBeDisabled();
	}
};
