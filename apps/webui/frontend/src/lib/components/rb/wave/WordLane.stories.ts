import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { KaraokeWord } from '$lib/api-karaoke';

import { WAVE_WINDOW_S } from './wave-window';
import WordLane from './WordLane.svelte';

/**
 * Karaoke word lane over one deck waveform. It fills its positioned parent
 * (inset 0), so the decorator gives the story root a fixed frame, the same
 * pattern as ScrubLyricStrip. The words are invented; positionMs picks the
 * active word.
 */
const TEXT = ['Hold', 'the', 'light', 'above', 'the', 'water', 'line', 'we', 'drift', 'home'];
const words: KaraokeWord[] = TEXT.map((word, idx) => ({
	idx,
	word,
	start_s: idx * 0.6,
	end_s: idx * 0.6 + 0.5,
	line_final: idx === 4 || idx === 9
}));

const meta = {
	title: 'Components/WordLane',
	component: WordLane,
	tags: ['autodocs', 'capture-canvas-width'],
	decorators: [
		(story) => {
			const root = typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.position = 'relative';
				root.style.width = '600px';
				root.style.height = '80px';
			}
			return story();
		}
	],
	args: { words, positionMs: 0, pitch: 1, windowSeconds: WAVE_WINDOW_S, playheadFrac: 0.5 }
} satisfies Meta<typeof WordLane>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Before the first word: the lane is laid out, nothing is active yet. */
export const BeforeFirstWord: Story = {};

/** Mid-line: the word sung at 2.4 s is the active one. */
export const MidLine: Story = { args: { positionMs: 2400 } };

/** Past the last word: no word is in the window, so the lane shows nothing. */
export const PastEnd: Story = { tags: ['renders-nothing'], args: { positionMs: 60000 } };
