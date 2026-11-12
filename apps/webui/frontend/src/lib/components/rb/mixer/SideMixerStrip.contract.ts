import { expect, fireEvent, fn, waitFor } from 'storybook/test';

import { deckStates, mixerState } from '$lib/player/state.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { EqBand } from '$lib/rb/mixer-types';
import type { StemControl, StemDeckState } from '$lib/rb/stem-types';

type SideMixerSide = 'outer-left' | 'outer-right';

type Args = {
	deckId: DeckId;
	side: SideMixerSide;
	mixerStems: boolean;
	oneq: (band: EqBand, value: number) => void;
	onfader: (value: number) => void;
	onStemGain: (stem: StemControl, value: number) => void;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

/**
 * Shared contract plays for SideMixerStrip.stories.ts (and a future React twin).
 * #9435 (epic #9371, MX-2).
 */

const DECK: DeckId = 2;
const DECKS: readonly DeckId[] = [1, 2, 3, 4];

const ctl = () => ({ muted: false, solo: false, gain: 0.5, fx_send: 0 });

function stems(): StemDeckState {
	return {
		status: 'ready',
		source: 'demucs',
		model: 'htdemucs',
		layout: 'demucs4',
		available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
		alignment: null,
		controls: { vocal: ctl(), instrumental: ctl(), drums: ctl(), bass: ctl(), other: ctl() },
		error: null,
		load: null
	};
}

const writeEq = (band: EqBand, value: number) => {
	const ch = mixerState.channels[DECK];
	if (band === 'low') ch.eq_low = value;
	else if (band === 'mid') ch.eq_mid = value;
	else if (band === 'high') ch.eq_high = value;
	else throw new Error(`unknown EQ band ${String(band)}`);
};
const writeFader = (value: number) => {
	mixerState.channels[DECK].fader = value;
};
const writeGain = (stem: StemControl, value: number) => {
	deckStates[DECK].stems.controls[stem].gain = value;
};

function sizeCanvas(canvasElement: HTMLElement): void {
	canvasElement.style.width = '150px';
	canvasElement.style.height = '360px';
}

const dial = (root: HTMLElement, label: string): HTMLElement => {
	const el = root.querySelector<HTMLElement>(`[role="slider"][aria-label="${label}"]`);
	if (el === null) throw new Error(`no dial "${label}"`);
	return el;
};

function columnOrder(root: HTMLElement): string[] {
	return [...root.querySelectorAll<HTMLElement>('[data-column]')].map(
		(el) => el.dataset.column ?? ''
	);
}

function column(root: HTMLElement, name: string): HTMLElement {
	const el = root.querySelector<HTMLElement>(`[data-column="${name}"]`);
	if (el === null) throw new Error(`no ${name} column`);
	return el;
}

type Snapshot = Record<number, { eq_high: number; eq_mid: number; eq_low: number; fader: number }>;
function snapshot(): Snapshot {
	const out: Snapshot = {};
	for (const id of DECKS) {
		const { eq_high, eq_mid, eq_low, fader } = mixerState.channels[id];
		out[id] = { eq_high, eq_mid, eq_low, fader };
	}
	return out;
}

const stemDialLabels = ['vocal', 'inst', 'bass', 'drums'] as const;

async function assertMirrorLayout(
	canvasElement: HTMLElement,
	columns: readonly string[],
	faderLeftOfEq: boolean
) {
	sizeCanvas(canvasElement);
	await expect(columnOrder(canvasElement)).toEqual([...columns]);
	const levelGeom = column(canvasElement, 'level').getBoundingClientRect();
	const eqGeom = column(canvasElement, 'eq').getBoundingClientRect();
	if (faderLeftOfEq) await expect(levelGeom.left).toBeLessThan(eqGeom.left);
	else await expect(levelGeom.left).toBeGreaterThan(eqGeom.left);
	for (const label of stemDialLabels) {
		await expect(
			column(canvasElement, 'stem').querySelector(`[aria-label="${label} stem level deck 2"]`)
		).not.toBeNull();
	}
}

const baseArgs = {
	deckId: DECK,
	mixerStems: true,
	oneq: fn(writeEq),
	onfader: fn(writeFader),
	onStemGain: fn(writeGain)
} satisfies Omit<Args, 'side'>;

export const contracts = {
	OuterLeftDefault: {
		args: { ...baseArgs, side: 'outer-left' as const },
		globals: { skin: 'default' },
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(
				canvasElement.querySelector('[data-testid="side-mixer-strip-2"]')
			).not.toBeNull();
			await assertMirrorLayout(canvasElement, ['level', 'eq', 'stem'], true);
			const before = snapshot();
			const low = dial(canvasElement, 'low EQ deck 2');
			low.focus();
			for (let i = 0; i < 3; i++) await fireEvent.keyDown(low, { key: 'ArrowUp' });
			await expect(args.oneq).toHaveBeenCalledWith('low', expect.any(Number));
			await waitFor(() => expect(mixerState.channels[2].eq_low).toBeGreaterThan(0.5));
			await waitFor(() =>
				expect(Number(dial(canvasElement, 'low EQ deck 2').getAttribute('aria-valuenow'))).toBe(
					mixerState.channels[2].eq_low
				)
			);
			const after = snapshot();
			for (const id of DECKS) {
				const expected = id === 2 ? { ...before[id], eq_low: after[2].eq_low } : before[id];
				await expect(after[id]).toEqual(expected);
			}
		}
	},
	OuterRightDefault: {
		args: { ...baseArgs, side: 'outer-right' as const },
		globals: { skin: 'default' },
		play: async ({ canvasElement }: Pick<Ctx, 'canvasElement'>) => {
			await expect(
				canvasElement.querySelector('[data-testid="side-mixer-strip-2"]')
			).not.toBeNull();
			await assertMirrorLayout(canvasElement, ['stem', 'eq', 'level'], false);
		}
	},
	OuterLeftGothic: {
		args: { ...baseArgs, side: 'outer-left' as const },
		globals: { skin: 'mono-dev' },
		play: async ({ canvasElement }: Pick<Ctx, 'canvasElement'>) => {
			await expect(
				canvasElement.querySelector('[data-testid="side-mixer-strip-2"]')
			).not.toBeNull();
			await assertMirrorLayout(canvasElement, ['level', 'eq', 'stem'], true);
		}
	},
	OuterRightGothic: {
		args: { ...baseArgs, side: 'outer-right' as const },
		globals: { skin: 'mono-dev' },
		play: async ({ canvasElement }: Pick<Ctx, 'canvasElement'>) => {
			await expect(
				canvasElement.querySelector('[data-testid="side-mixer-strip-2"]')
			).not.toBeNull();
			await assertMirrorLayout(canvasElement, ['stem', 'eq', 'level'], false);
		}
	},
	OuterLeftLight: {
		args: { ...baseArgs, side: 'outer-left' as const },
		globals: { skin: 'light' },
		play: async ({ canvasElement }: Pick<Ctx, 'canvasElement'>) => {
			await expect(
				canvasElement.querySelector('[data-testid="side-mixer-strip-2"]')
			).not.toBeNull();
			await assertMirrorLayout(canvasElement, ['level', 'eq', 'stem'], true);
		}
	},
	OuterRightLight: {
		args: { ...baseArgs, side: 'outer-right' as const },
		globals: { skin: 'light' },
		play: async ({ canvasElement }: Pick<Ctx, 'canvasElement'>) => {
			await expect(
				canvasElement.querySelector('[data-testid="side-mixer-strip-2"]')
			).not.toBeNull();
			await assertMirrorLayout(canvasElement, ['stem', 'eq', 'level'], false);
		}
	}
};
