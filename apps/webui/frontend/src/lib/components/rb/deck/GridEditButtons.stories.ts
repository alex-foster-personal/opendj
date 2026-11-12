import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fireEvent, fn, userEvent, within } from 'storybook/test';

import type { AnlzBeatgridEdit, AnlzData } from '$lib/rb/anlz-types';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemDeckState } from '$lib/rb/stem-types';
import { GRID_ADJUST_STEP_MS } from '$lib/rb/grid-edit';

import GridEditButtons from './GridEditButtons.svelte';

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

const BPM = 128;
const beats = Array.from({ length: 8 }, (_, i) => ({
	n: (i % 4) + 1,
	bpm: BPM,
	t: (i * 60) / BPM
}));

const grid = (edit?: AnlzBeatgridEdit): AnlzData =>
	({
		stable_id: 'a'.repeat(40),
		points: 1200,
		waveform: {} as never,
		beatgrid: {
			source: 'rekordbox',
			beat_count: beats.length,
			beats,
			...(edit !== undefined ? { edit } : {})
		},
		cues: [],
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
	anlz: grid(),
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
	title: 'Contracts/Deck/GridEditButtons',
	component: GridEditButtons,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	args: {
		deck: deckState(),
		deckId: 1,
		press: fn(),
		reportError: fn()
	}
} satisfies Meta<typeof GridEditButtons>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = {
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		const adjust = canvas.getByTestId('grid-adjust-deck-1');
		const shift = canvas.getByTestId('grid-shift-deck-1');
		await expect(adjust).toBeEnabled();
		await expect(shift).toBeEnabled();
		await expect(adjust).not.toHaveClass('lit');
		await expect(shift).not.toHaveClass('lit');
		await expect(adjust).toHaveAttribute('data-offset-ms', '0');
		await expect(shift).toHaveAttribute('data-beat-shift', '0');
		adjust.focus();
		await userEvent.keyboard('{ArrowRight}');
		await expect(args.press).toHaveBeenCalledWith({
			kind: 'nudge',
			offset_ms: GRID_ADJUST_STEP_MS,
			beats: 0
		});
		const rect = shift.getBoundingClientRect();
		fireEvent.click(shift, { clientX: rect.left + rect.width * 0.75, detail: 1 });
		await expect(args.press).toHaveBeenCalledWith({ kind: 'nudge', offset_ms: 0, beats: 1 });
		await expect(args.reportError).not.toHaveBeenCalled();
	}
};

export const Editing: Story = {
	args: {
		deck: deckState({
			anlz: grid({ source: 'rekordbox', offset_ms: 12, beat_shift: 1 })
		})
	},
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		const adjust = canvas.getByTestId('grid-adjust-deck-1');
		const shift = canvas.getByTestId('grid-shift-deck-1');
		await expect(adjust).toHaveClass('lit');
		await expect(shift).toHaveClass('lit');
		await expect(adjust).toHaveAttribute('data-offset-ms', '12');
		await expect(shift).toHaveAttribute('data-beat-shift', '1');
		await expect(adjust).toHaveAttribute('title', expect.stringContaining('Now:'));
		await expect(shift).toHaveAttribute('title', expect.stringContaining('Now:'));
		fireEvent.click(shift, { altKey: true, detail: 1 });
		await expect(args.press).toHaveBeenCalledWith({ kind: 'reset', part: 'shift' });
	}
};

export const DisabledNoGrid: Story = {
	args: { deck: deckState({ anlz: null }) },
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		const adjust = canvas.getByTestId('grid-adjust-deck-1');
		const shift = canvas.getByTestId('grid-shift-deck-1');
		await expect(adjust).toBeDisabled();
		await expect(shift).toBeDisabled();
		await expect(adjust).toHaveAttribute(
			'title',
			expect.stringContaining('Unavailable: the beatgrid has not loaded yet.')
		);
		fireEvent.click(adjust);
		await expect(args.press).not.toHaveBeenCalled();
	}
};

export const DisabledUsbTrack: Story = {
	args: { deck: deckState({ stable_id: `usb-${'a'.repeat(36)}` }) },
	play: async ({ canvasElement }) => {
		const adjust = within(canvasElement).getByTestId('grid-adjust-deck-1');
		await expect(adjust).toBeDisabled();
		await expect(adjust).toHaveAttribute(
			'title',
			expect.stringContaining('plays from a USB stick')
		);
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
		const adjust = within(canvasElement).getByTestId('grid-adjust-deck-1');
		await expect(adjust).toBeDisabled();
		await expect(adjust).toHaveAttribute(
			'title',
			expect.stringContaining('no track is loaded on this deck')
		);
	}
};
