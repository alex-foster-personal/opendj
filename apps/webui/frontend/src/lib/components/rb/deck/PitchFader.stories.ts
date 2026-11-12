import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fireEvent, fn, userEvent, waitFor, within } from 'storybook/test';

import type { PitchRange } from '$lib/player/constants';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemDeckState } from '$lib/rb/stem-types';

import PitchFader from './PitchFader.svelte';
import { MIN_TEMPO_RATIO } from './pitch-fader-geometry';
import PitchFaderStoryDock from './PitchFaderStoryDock.svelte';

const TRACK_TITLE = 'Night Drive (Extended Mix)';
const THUMB_H = 12;

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
	anlz: null,
	anlz_error: null,
	last_load_latency_ms: 850,
	load_generation: 1,
	last_load_stages: null,
	is_master: false,
	pending_loop_in_ms: null,
	last_loop: null,
	...over
});

function pitchFor(range: PitchRange, position: 'centre' | 'top' | 'bottom'): number {
	if (position === 'centre') return 1;
	if (position === 'top') return 1 + range / 100;
	return range === 100 ? MIN_TEMPO_RATIO : 1 - range / 100;
}

const meta = {
	title: 'Contracts/Deck/PitchFader',
	component: PitchFader,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	render: (args) => ({ Component: PitchFaderStoryDock, props: args }),
	args: {
		deck: deckState({ pitch: 1 }),
		pitchRange: 16 as PitchRange,
		pending: false,
		onTempoChange: fn(async () => {}),
		onRangeChange: fn(async () => {})
	}
} satisfies Meta<typeof PitchFader>;

export default meta;
type Story = StoryObj<typeof meta>;

async function assertRangePosition(
	canvasElement: HTMLElement,
	range: PitchRange,
	position: 'centre' | 'top' | 'bottom'
): Promise<void> {
	const slider = canvasElement.querySelector<HTMLElement>('[data-testid="pitch-fader-deck-1"]')!;
	const thumb = slider.querySelector<HTMLElement>('.rb-fader-thumb')!;
	const now = position === 'centre' ? '0' : position === 'top' ? String(range) : String(-range);
	await expect(slider).toHaveAttribute('aria-valuemin', String(-range));
	await expect(slider).toHaveAttribute('aria-valuemax', String(range));
	await expect(slider).toHaveAttribute('aria-valuenow', now);
	for (const r of [8, 16, 100] as const) {
		const btn = canvasElement.querySelector(`[data-testid="pitch-range-${r}-deck-1"]`);
		await expect(btn).toHaveAttribute('aria-pressed', r === range ? 'true' : 'false');
	}
	const trackH = slider.getBoundingClientRect().height;
	const topPx = Number.parseFloat(thumb.style.top);
	if (position === 'top') {
		await expect(topPx).toBeLessThan(1);
	} else if (position === 'bottom') {
		await expect(topPx).toBeGreaterThanOrEqual(trackH - THUMB_H - 1);
		await expect(topPx).toBeLessThanOrEqual(trackH - THUMB_H + 1);
	} else {
		const centre = (trackH - THUMB_H) / 2;
		await expect(topPx).toBeGreaterThanOrEqual(centre - 1);
		await expect(topPx).toBeLessThanOrEqual(centre + 1);
	}
}

export const Range8Centre: Story = {
	args: { pitchRange: 8, deck: deckState({ pitch: pitchFor(8, 'centre') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 8, 'centre');
		await expect(canvasElement.querySelector('[data-testid="pitch-fader-deck-1"]')).not.toBeNull();
	}
};

export const Range8Top: Story = {
	args: { pitchRange: 8, deck: deckState({ pitch: pitchFor(8, 'top') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 8, 'top');
		await expect(canvasElement.querySelector('.rb-fader-thumb')).not.toBeNull();
	}
};

export const Range8Bottom: Story = {
	args: { pitchRange: 8, deck: deckState({ pitch: pitchFor(8, 'bottom') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 8, 'bottom');
		await expect(
			canvasElement.querySelector('[data-testid="pitch-range-8-deck-1"]')
		).toHaveAttribute('aria-pressed', 'true');
	}
};

export const Range16Centre: Story = {
	args: { pitchRange: 16, deck: deckState({ pitch: pitchFor(16, 'centre') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 16, 'centre');
		await expect(canvasElement).toBeTruthy();
	}
};

export const Range16Top: Story = {
	args: { pitchRange: 16, deck: deckState({ pitch: pitchFor(16, 'top') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 16, 'top');
		await expect(canvasElement.querySelector('[data-testid="pitch-fader-deck-1"]')).toHaveAttribute(
			'aria-valuenow',
			'16'
		);
	}
};

export const Range16Bottom: Story = {
	args: { pitchRange: 16, deck: deckState({ pitch: pitchFor(16, 'bottom') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 16, 'bottom');
		await expect(canvasElement.querySelector('[data-testid="pitch-fader-deck-1"]')).toHaveAttribute(
			'aria-valuenow',
			'-16'
		);
	}
};

export const WideCentre: Story = {
	args: { pitchRange: 100, deck: deckState({ pitch: pitchFor(100, 'centre') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 100, 'centre');
		await expect(
			canvasElement.querySelector('[data-testid="pitch-range-100-deck-1"]')
		).toHaveAttribute('aria-pressed', 'true');
	}
};

export const WideTop: Story = {
	args: { pitchRange: 100, deck: deckState({ pitch: pitchFor(100, 'top') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 100, 'top');
		await expect(canvasElement.querySelector('[data-testid="pitch-fader-deck-1"]')).toHaveAttribute(
			'aria-valuenow',
			'100'
		);
	}
};

export const WideBottom: Story = {
	args: { pitchRange: 100, deck: deckState({ pitch: pitchFor(100, 'bottom') }) },
	play: async ({ canvasElement }) => {
		await assertRangePosition(canvasElement, 100, 'bottom');
		await expect(canvasElement.querySelector('[data-testid="pitch-fader-deck-1"]')).toHaveAttribute(
			'aria-valuenow',
			'-100'
		);
	}
};

/**
 * Drag to the top of the 16% range: tempo callback receives 1.16 and the slider
 * readout is aria-valuenow="16" (not JogDial's +16.0% text).
 */
export const DragToTop16: Story = {
	args: { pitchRange: 16, deck: deckState({ pitch: 1 }) },
	play: async ({ canvasElement, args }) => {
		const slider = canvasElement.querySelector<HTMLElement>('[data-testid="pitch-fader-deck-1"]')!;
		Object.assign(slider, {
			setPointerCapture: () => {},
			releasePointerCapture: () => {},
			hasPointerCapture: () => false
		});
		const rect = slider.getBoundingClientRect();
		const midX = rect.left + rect.width / 2;
		const midY = rect.top + rect.height / 2;
		fireEvent.pointerDown(slider, { pointerId: 1, button: 0, clientX: midX, clientY: midY });
		fireEvent.pointerMove(slider, {
			pointerId: 1,
			buttons: 1,
			clientX: midX,
			clientY: rect.top - 40
		});
		fireEvent.pointerUp(slider, { pointerId: 1, button: 0, clientX: midX, clientY: rect.top - 40 });
		await waitFor(() =>
			expect(args.onTempoChange).toHaveBeenLastCalledWith(expect.closeTo(1.16, 6))
		);
		await waitFor(() => expect(slider).toHaveAttribute('aria-valuenow', '16'));
		const thumb = slider.querySelector<HTMLElement>('.rb-fader-thumb')!;
		await expect(Number.parseFloat(thumb.style.top)).toBeLessThan(1);
	}
};

export const PickRange8: Story = {
	args: { pitchRange: 16, deck: deckState({ pitch: 1 }) },
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		await userEvent.click(canvas.getByTestId('pitch-range-8-deck-1'));
		await expect(args.onRangeChange).toHaveBeenCalledTimes(1);
		await expect(args.onRangeChange).toHaveBeenCalledWith(8);
		await waitFor(() =>
			expect(canvas.getByTestId('pitch-fader-deck-1')).toHaveAttribute('aria-valuemax', '8')
		);
		await expect(canvas.getByTestId('pitch-range-8-deck-1')).toHaveAttribute(
			'aria-pressed',
			'true'
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
			position_ms: 0
		})
	},
	play: async ({ canvasElement, args }) => {
		const slider = canvasElement.querySelector<HTMLElement>('[data-testid="pitch-fader-deck-1"]')!;
		await expect(slider).toHaveAttribute('aria-disabled', 'true');
		Object.assign(slider, {
			setPointerCapture: () => {},
			releasePointerCapture: () => {},
			hasPointerCapture: () => false
		});
		const rect = slider.getBoundingClientRect();
		fireEvent.pointerDown(slider, {
			pointerId: 1,
			button: 0,
			clientX: rect.left + rect.width / 2,
			clientY: rect.top + rect.height / 2
		});
		await expect(args.onTempoChange).not.toHaveBeenCalled();
	}
};
