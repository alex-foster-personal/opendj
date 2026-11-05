import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fireEvent, fn, waitFor } from 'storybook/test';

import { initRustMode, rustMode } from '$lib/audio-engine/rust-mode.svelte';
import { watchRustInert } from '$lib/audio-engine/rust-inert';
import { deckStates } from '$lib/player/state.svelte';
import { colorFx } from '$lib/rb/color-fx.svelte';
import { mixerColumnPx } from '$lib/rb/mixer-width';
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';
import ChannelStrip from './ChannelStrip.svelte';

/**
 * MIXUX-14 (the maintainer, Mon 2 Nov 2026): the mixer's S button stands stem level
 * dials beside the EQ. These contracts turn a stem dial for real in Chromium
 * (drag, wheel, arrow keys) and measure the layout at the strip width the
 * widened mixer gives one channel.
 *
 * The strip reads its deck's stems from the shared store, so each story writes
 * deckStates[1].stems first, and the onStemGain fixture writes the new level
 * back the way the engine does, so the dial's own value is what is asserted.
 */

const ctl = () => ({ muted: false, solo: false, gain: 0.5 });

function stems(layout: 'demucs4' | 'roformer2'): StemDeckState {
	return {
		status: 'ready',
		source: layout === 'demucs4' ? 'demucs' : 'roformer',
		model: layout === 'demucs4' ? 'htdemucs' : 'roformer',
		layout,
		available_controls:
			layout === 'demucs4' ? ['vocal', 'instrumental', 'drums', 'bass', 'other'] : ['vocal', 'instrumental'],
		alignment: null,
		controls: { vocal: ctl(), instrumental: ctl(), drums: ctl(), bass: ctl(), other: ctl() },
		error: null,
		load: null
	};
}

/** One channel's width inside the widened mixer: column minus 6px padding each side, three 2px gaps, over four. */
const STRIP_PX = (mixerColumnPx(true) - 12 - 6) / 4;

const writeGain = (stem: StemControl, value: number) => {
	deckStates[1].stems.controls[stem].gain = value;
};

const baseArgs = {
	deckId: 1 as const,
	less: false,
	trim: 0.5,
	eqHigh: 0.5,
	eqMid: 0.5,
	eqLow: 0.5,
	filter: 0.5,
	fader: 1,
	cueEnabled: false,
	mixerStems: true,
	stemPending: false,
	ontrim: fn(),
	onAutoGainOffset: fn(async () => ({ ok: true })),
	oneq: fn(),
	onfilter: fn(),
	oncolorfx: fn(),
	onfader: fn(),
	oncue: fn(),
	onStemGain: fn(writeGain),
	onStemMute: fn(async () => {}),
	onStemSolo: fn(async () => {})
};

const meta = {
	title: 'Contracts/Mixer/ChannelStrip stems',
	component: ChannelStrip,
	args: baseArgs
} satisfies Meta<typeof ChannelStrip>;
export default meta;
type Story = StoryObj<typeof meta>;

const dial = (root: HTMLElement, label: string): HTMLElement => {
	const el = root.querySelector<HTMLElement>(`[role="slider"][aria-label="${label}"]`);
	if (el === null) throw new Error(`no dial "${label}"`);
	return el;
};
const valueOf = (el: HTMLElement): number => Number(el.getAttribute('aria-valuenow'));

function sizeCanvas(canvasElement: HTMLElement): void {
	canvasElement.style.width = `${STRIP_PX}px`;
	canvasElement.style.height = '520px';
}

/** the maintainer, Wed 4 Nov 2026: the DRUMS dial (beside FILTER) did not turn on drag, while the wheel did.
 * Every stem dial, drums included, turns by a pointer drag. */
export const StemsDrumsDragTurns: Story = {
	beforeEach: () => {
		deckStates[1].stems = stems('demucs4');
	},
	play: async ({ canvasElement, args }) => {
		sizeCanvas(canvasElement);
		for (const [label, stem] of [
			['drums stem level deck 1', 'drums'],
			['bass stem level deck 1', 'bass']
		] as const) {
			const knob = dial(canvasElement, label);
			const box = knob.getBoundingClientRect();
			const x = box.left + box.width / 2;
			const y = box.top + box.height / 2;
			// A real press lands on whatever is on top at that point, not on the dial
			// fireEvent targets: nothing (caption, chevron, menu, chip) may cover it.
			for (const [px, py] of [[x, y], [box.left + 3, y], [box.right - 3, y], [x, box.top + 3], [x, box.bottom - 3]]) {
				const hit = document.elementFromPoint(px, py);
				await expect(hit !== null && knob.contains(hit), `${label} covered at ${px},${py} by ${hit?.outerHTML.slice(0, 120)}`).toBe(true);
			}
			await fireEvent.pointerDown(knob, { pointerId: 1, button: 0, clientX: x, clientY: y });
			for (let i = 1; i <= 10; i++) {
				await fireEvent.pointerMove(knob, { pointerId: 1, buttons: 1, clientX: x, clientY: y - i * 6 });
			}
			await fireEvent.pointerUp(knob, { pointerId: 1, button: 0, clientX: x, clientY: y - 60 });
			await waitFor(() => expect(valueOf(dial(canvasElement, label))).toBeGreaterThan(0.5));
			await expect(args.onStemGain).toHaveBeenCalledWith(stem, expect.any(Number));
		}
		await expect(args.onfilter).not.toHaveBeenCalled();
	}
};

/** S on, four-stem bundle: every stem dial turns by drag, wheel and keys, and sits where the maintainer asked. */
export const StemsOnTurnAndLayout: Story = {
	beforeEach: () => {
		deckStates[1].stems = stems('demucs4');
	},
	play: async ({ canvasElement, args }) => {
		sizeCanvas(canvasElement);
		const vocal = dial(canvasElement, 'vocal stem level deck 1');
		const hi = dial(canvasElement, 'high EQ deck 1');

		// Drag up: the VOCAL level rises and stays the VOCAL dial throughout.
		const box = vocal.getBoundingClientRect();
		const x = box.left + box.width / 2;
		const y = box.top + box.height / 2;
		await fireEvent.pointerDown(vocal, { pointerId: 1, button: 0, clientX: x, clientY: y });
		for (let i = 1; i <= 10; i++) {
			await fireEvent.pointerMove(vocal, { pointerId: 1, buttons: 1, clientX: x, clientY: y - i * 6 });
		}
		await fireEvent.pointerUp(vocal, { pointerId: 1, button: 0, clientX: x, clientY: y - 60 });
		await waitFor(() => expect(valueOf(dial(canvasElement, 'vocal stem level deck 1'))).toBeGreaterThan(0.5));
		await expect(args.onStemGain).toHaveBeenCalledWith('vocal', expect.any(Number));
		await expect(args.oneq).not.toHaveBeenCalled();
		await expect(valueOf(hi)).toBe(0.5);

		// Wheel down lowers BASS; ArrowUp raises DRUMS.
		const bass = dial(canvasElement, 'bass stem level deck 1');
		await fireEvent.wheel(bass, { deltaY: 100, deltaMode: 0 });
		await waitFor(() => expect(valueOf(dial(canvasElement, 'bass stem level deck 1'))).toBeLessThan(0.5));
		const drums = dial(canvasElement, 'drums stem level deck 1');
		drums.focus();
		await fireEvent.keyDown(drums, { key: 'ArrowUp' });
		await waitFor(() => expect(valueOf(dial(canvasElement, 'drums stem level deck 1'))).toBeGreaterThan(0.5));
		await expect(args.onStemGain).toHaveBeenCalledWith('bass', expect.any(Number));
		await expect(args.onStemGain).toHaveBeenCalledWith('drums', expect.any(Number));

		// Layout: VOCAL/INST/BASS beside HI/MID/LOW row for row; DRUMS beside FILTER.
		const rows: [string, string][] = [
			['high EQ deck 1', 'vocal stem level deck 1'],
			['mid EQ deck 1', 'inst stem level deck 1'],
			['low EQ deck 1', 'bass stem level deck 1'],
			['filter deck 1', 'drums stem level deck 1']
		];
		for (const [eqLabel, stemLabel] of rows) {
			const eq = dial(canvasElement, eqLabel).getBoundingClientRect();
			const st = dial(canvasElement, stemLabel).getBoundingClientRect();
			await expect(st.left).toBeGreaterThanOrEqual(eq.right);
			await expect(Math.abs(st.top - eq.top)).toBeLessThan(2);
		}
		// Everything fits the strip width the widened mixer gives one channel.
		const strip = canvasElement.querySelector<HTMLElement>('.strip');
		if (strip === null) throw new Error('no strip');
		await expect(strip.scrollWidth).toBeLessThanOrEqual(Math.ceil(strip.clientWidth));
	}
};

/** S on, RoFormer bundle: VOCAL and INST only; no BASS or DRUMS dial. */
export const StemsOnRoformer: Story = {
	beforeEach: () => {
		deckStates[1].stems = stems('roformer2');
	},
	play: async ({ canvasElement }) => {
		sizeCanvas(canvasElement);
		await expect(canvasElement.querySelector('[aria-label="vocal stem level deck 1"]')).not.toBeNull();
		await expect(canvasElement.querySelector('[aria-label="inst stem level deck 1"]')).not.toBeNull();
		await expect(canvasElement.querySelector('[aria-label="bass stem level deck 1"]')).toBeNull();
		await expect(canvasElement.querySelector('[aria-label="drums stem level deck 1"]')).toBeNull();
	}
};

/** S off: the strip is the plain EQ strip, stems or not. */
export const StemsOff: Story = {
	args: { ...baseArgs, mixerStems: false },
	beforeEach: () => {
		deckStates[1].stems = stems('demucs4');
	},
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('[aria-label$="stem level deck 1"]')).toHaveLength(0);
		await expect(canvasElement.querySelector('[aria-label="high EQ deck 1"]')).not.toBeNull();
	}
};

/** STEMDIAL-DRAG-01 (the maintainer, Wed 4 Nov 2026, live: the DRUMS dial did not turn on drag, the others did).
 * DRUMS sits in the colour FX slot, and on a SPACE or ECHO channel Rust engine mode held that WHOLE
 * slot inert (rust-inert's capture guard ate the press), so the stem dial froze with it. Only the
 * colour knob is a Web-Audio-only control: DRUMS sends stem_gain, which the engine plays. */
export const StemsDrumsDragTurnsOnRustSpace: Story = {
	beforeEach: () => {
		deckStates[1].stems = stems('demucs4');
		initRustMode();
		const wasRust = rustMode.enabled;
		rustMode.enabled = true;
		colorFx[1].effect = 'space';
		return () => {
			rustMode.enabled = wasRust;
			colorFx[1].effect = 'filter';
		};
	},
	play: async ({ canvasElement, args }) => {
		sizeCanvas(canvasElement);
		const inert = watchRustInert(document.body);
		try {
			const filter = dial(canvasElement, 'filter deck 1');
			await waitFor(() => expect(filter.closest('[data-rust-inert]')).not.toBeNull());
			const drums = dial(canvasElement, 'drums stem level deck 1');
			await expect(drums.closest('[data-rust-inert]'), 'the DRUMS dial is not under the colour FX hold').toBeNull();
			const box = drums.getBoundingClientRect();
			const x = box.left + box.width / 2;
			const y = box.top + box.height / 2;
			await fireEvent.pointerDown(drums, { pointerId: 1, button: 0, clientX: x, clientY: y });
			for (let i = 1; i <= 10; i++) {
				await fireEvent.pointerMove(drums, { pointerId: 1, buttons: 1, clientX: x, clientY: y - i * 6 });
			}
			await fireEvent.pointerUp(drums, { pointerId: 1, button: 0, clientX: x, clientY: y - 60 });
			await waitFor(() => expect(valueOf(dial(canvasElement, 'drums stem level deck 1'))).toBeGreaterThan(0.5));
			await expect(args.onStemGain).toHaveBeenCalledWith('drums', expect.any(Number));
			// Control: the SPACE colour knob itself is still held inert under Rust.
			const fb = filter.getBoundingClientRect();
			await fireEvent.pointerDown(filter, { pointerId: 1, button: 0, clientX: fb.left + 5, clientY: fb.top + 5 });
			await fireEvent.pointerMove(filter, { pointerId: 1, buttons: 1, clientX: fb.left + 5, clientY: fb.top - 55 });
			await fireEvent.pointerUp(filter, { pointerId: 1, button: 0, clientX: fb.left + 5, clientY: fb.top - 55 });
			await expect(args.onfilter).not.toHaveBeenCalled();
		} finally {
			inert.stop();
		}
	}
};

/** COLORFX-8341 kept: a Cmd-click (Ctrl off a Mac) on the colour knob still cycles the effect, while the
 * same press on the DRUMS dial beside it is the dial's, never a colour FX cycle. */
export const ColorFxCycleOnKnobNotDrums: Story = {
	beforeEach: () => {
		deckStates[1].stems = stems('demucs4');
	},
	play: async ({ canvasElement, args }) => {
		sizeCanvas(canvasElement);
		const mac = /mac|iphone|ipad/i.test(navigator.platform);
		const mod = mac ? { metaKey: true } : { ctrlKey: true };
		const drums = dial(canvasElement, 'drums stem level deck 1');
		await fireEvent.pointerDown(drums, { pointerId: 1, button: 0, ...mod });
		await fireEvent.pointerUp(drums, { pointerId: 1, button: 0, ...mod });
		await fireEvent.click(drums, { button: 0, ...mod });
		await expect(args.oncolorfx).not.toHaveBeenCalled();
		const filter = dial(canvasElement, 'filter deck 1');
		await fireEvent.pointerDown(filter, { pointerId: 1, button: 0, ...mod });
		await fireEvent.pointerUp(filter, { pointerId: 1, button: 0, ...mod });
		await fireEvent.click(filter, { button: 0, ...mod });
		await expect(args.oncolorfx).toHaveBeenCalledWith('next');
		await expect(args.onfilter).not.toHaveBeenCalled();
	}
};
