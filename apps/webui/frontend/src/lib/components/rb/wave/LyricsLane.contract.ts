import { expect, within } from 'storybook/test';

import { WAVE_WINDOW_S } from './render';
import type { LyricLine } from './lyrics-lane';

/**
 * LyricsLane UI contract, shared by the Svelte story (LyricsLane.stories.ts)
 * and its React twin (LyricsLane.stories.tsx). WAVE_WINDOW_S is 24 s, so at
 * pitch 1 one percent of the lane is 240 ms and the playhead sits at 50%.
 * See src/react/README.md.
 */

type Args = {
	lyrics: { lines: LyricLine[] } | null;
	loadError: Error | null;
	positionMs: number;
	pitch: number;
	windowSeconds: number;
	playheadFrac: number;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const LINES: LyricLine[] = [
	{ start_ms: 0, text: 'Intro' },
	{ start_ms: 1000, text: 'early' },
	{ start_ms: 4000, text: 'A' },
	{ start_ms: 4000, text: 'B' },
	{ start_ms: 18000, text: 'Now' },
	{ start_ms: 25000, text: 'Next' },
	{ start_ms: 60000, text: 'Far' }
];

const spans = (root: HTMLElement): HTMLElement[] => Array.from(root.querySelectorAll<HTMLElement>('span.lyric-line'));

export const contracts = {
	/**
	 * Duets merge into one entry, rows alternate, the line at or before the
	 * playhead is active, and entries whose end has left the lane (Intro) or
	 * that start past it (Far) are not drawn.
	 */
	Playing: {
		args: { lyrics: { lines: LINES }, loadError: null, positionMs: 20000, pitch: 1, windowSeconds: WAVE_WINDOW_S, playheadFrac: 0.5 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const lane = within(canvasElement).getByLabelText('Synced lyrics');
			await expect(lane).toHaveClass('lyrics-lane');
			const drawn = spans(canvasElement);
			await expect(drawn.map((s) => s.textContent)).toEqual(['early', 'A / B', 'Now', 'Next']);
			await expect(drawn.map((s) => s.dataset.row)).toEqual(['1', '0', '1', '0']);
			await expect(drawn.map((s) => s.getAttribute('aria-current'))).toEqual([null, null, 'true', null]);
			await expect(drawn[2]).toHaveClass('active');
			await expect(drawn[1]).not.toHaveClass('active');
			await expect(parseFloat(drawn[2].style.left)).toBeCloseTo(50 + (18000 - 20000) / 240, 3);
			await expect(drawn[2].style.left.endsWith('%')).toBe(true);
			await expect(drawn[2].style.top).toBe('68%');
			await expect(drawn[1].style.top).toBe('32%');
			await expect(drawn[1].style.maxWidth).toMatch(/^calc\(87\.5% - 6px\)$/);
			await expect(drawn[3].style.maxWidth).toBe('');
			await expect(canvasElement.querySelector('.lyrics-error')).toBeNull();
		}
	},
	/** At double pitch the window covers twice the track time, so Far comes on screen and the active line moves. */
	DoublePitch: {
		args: { lyrics: { lines: LINES }, loadError: null, positionMs: 40000, pitch: 2, windowSeconds: WAVE_WINDOW_S, playheadFrac: 0.5 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const drawn = spans(canvasElement);
			await expect(drawn.map((s) => s.textContent)).toEqual(['early', 'A / B', 'Now', 'Next', 'Far']);
			await expect(drawn.find((s) => s.classList.contains('active'))?.textContent).toBe('Next');
			await expect(parseFloat(drawn[4].style.left)).toBeCloseTo(50 + (60000 - 40000) / 480, 3);
		}
	},
	/** A load error replaces the lane with an error tag that carries the message on hover. */
	LoadError: {
		args: { lyrics: { lines: LINES }, loadError: new Error('lyrics cache 503'), positionMs: 0, pitch: 1, windowSeconds: WAVE_WINDOW_S, playheadFrac: 0.5 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const err = within(canvasElement).getByText('LYRICS ERROR');
			await expect(err).toHaveClass('lyrics-error');
			await expect(err).toHaveAttribute('title', 'lyrics cache 503');
			await expect(canvasElement.querySelector('.lyrics-lane')).toBeNull();
		}
	},
	/** No lyrics and no error renders nothing. */
	NoLyrics: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { lyrics: null, loadError: null, positionMs: 0, pitch: 1, windowSeconds: WAVE_WINDOW_S, playheadFrac: 0.5 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('.lyrics-lane')).toBeNull();
			await expect(canvasElement.querySelector('.lyrics-error')).toBeNull();
		}
	}
};
