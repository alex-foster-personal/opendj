import { expect } from 'storybook/test';

import type { LyricWord } from '$lib/api';
import {
	indexLyricWords,
	resolvePointerWord,
	type PointerWord,
	type WordIndex
} from '$lib/lyrics/pointer-word';

/**
 * ScrubLyricStrip UI contract, shared by the Svelte story
 * (ScrubLyricStrip.stories.ts) and its React twin (ScrubLyricStrip.stories.tsx).
 * The pointer states come from the real resolver, not hand-built objects, so
 * the stories show what a strip would actually be handed. See src/react/README.md.
 */

type Args = { index: WordIndex; state: PointerWord; xPx: number; widthPx: number; radius?: number };
type Ctx = { canvasElement: HTMLElement; args: Args };

const TIMES: [string, number, number][] = [
	['plimba', 0.0, 0.4],
	['tavrex', 0.5, 0.8],
	['quonel', 0.9, 1.4],
	['mirdax', 3.0, 3.6],
	['selvup', 3.7, 4.0],
	['kexalo', 4.1, 4.5]
];
const WORDS = TIMES.map(
	([word, start_s, end_s], idx) => ({ idx, word, start_s, end_s, line_final: idx === 2 }) as LyricWord
);
const INDEX = indexLyricWords(WORDS) as WordIndex;
const at = (t: number): PointerWord => resolvePointerWord(INDEX, t, { nearS: 0.25 });

const readout = (root: HTMLElement): HTMLElement => root.querySelector('.scrub-readout') as HTMLElement;
const words = (root: HTMLElement): HTMLElement[] => Array.from(readout(root).querySelectorAll<HTMLElement>('span.w'));

export const contracts = {
	/** Inside a word: it is the focus, two dimmed neighbors either side, anchored at the pointer x. */
	InsideWord: {
		args: { index: INDEX, state: at(1.0), xPx: 120, widthPx: 400 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = readout(canvasElement);
			await expect(el).toHaveAttribute('data-kind', 'inside');
			await expect(el).toHaveAttribute('aria-hidden', 'true');
			await expect(el).toHaveAttribute('title', 'word under the pointer (aligned onset/offset seconds bound it)');
			await expect(el.style.left).toBe('120px');
			await expect(words(canvasElement).map((w) => w.textContent)).toEqual(['plimba', 'tavrex', 'quonel', 'mirdax', 'selvup']);
			await expect(words(canvasElement).map((w) => w.dataset.role)).toEqual(['dim', 'dim', 'focus', 'dim', 'dim']);
			await expect(el.querySelector('.gap-dot')).toBeNull();
			await expect(el.textContent).toBe('plimba tavrex quonel mirdax selvup ');
		}
	},
	/** Far into an instrumental gap: no focus, the gap is marked after the last sung word, x clamps to 8px. */
	FarGap: {
		args: { index: INDEX, state: at(2.2), xPx: -40, widthPx: 400 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = readout(canvasElement);
			await expect(el).toHaveAttribute('data-kind', 'gap');
			await expect(el).toHaveAttribute('title', 'pointer is in an instrumental gap between the dimmed words');
			await expect(el.style.left).toBe('8px');
			await expect(words(canvasElement).every((w) => w.dataset.role === 'dim')).toBe(true);
			const dots = el.querySelectorAll('.gap-dot');
			await expect(dots).toHaveLength(1);
			await expect(dots[0]).toHaveAttribute('title', 'the pointer is in this gap');
			await expect(dots[0].previousElementSibling).toHaveTextContent('quonel');
		}
	},
	/** Just before a word with radius 1: that word is the focus, one neighbor each side, x clamps to width - 8. */
	BeforeWordRadiusOne: {
		args: { index: INDEX, state: at(2.9), xPx: 500, widthPx: 400, radius: 1 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = readout(canvasElement);
			await expect(el).toHaveAttribute('data-kind', 'before');
			await expect(el).toHaveAttribute('title', 'pointer is just before this word');
			await expect(el.style.left).toBe('392px');
			await expect(words(canvasElement).map((w) => w.textContent)).toEqual(['quonel', 'mirdax', 'selvup']);
			await expect(words(canvasElement)[1]).toHaveAttribute('data-role', 'focus');
		}
	},
	/** Nothing to point at (no focus and no neighbor): no readout at all. */
	NothingToShow: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { index: INDEX, state: resolvePointerWord(null, 1.0, { nearS: 0.25 }), xPx: 50, widthPx: 400 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('.scrub-readout')).toBeNull();
		}
	}
};
