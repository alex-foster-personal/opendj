import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import type { LyricWord } from '$lib/api';
import { indexLyricWords, resolvePointerWord, type WordIndex } from '$lib/lyrics/pointer-word';

import { contracts } from './ScrubLyricStrip.contract';
import ScrubLyricStrip from './ScrubLyricStrip.svelte';

/**
 * The readout anchors above a position:relative parent (bottom: 100%). The
 * decorator gives the story root that parent plus headroom, standing in for
 * the PreviewStrip row the app mounts it in.
 */
const TEXT = ['Hold', 'the', 'light', 'above', 'the', 'water', 'line', 'we', 'drift', 'home'];
const words: LyricWord[] = TEXT.map((word, idx) => ({
	idx,
	word,
	start_s: idx < 5 ? 10 + idx * 0.5 : 20 + (idx - 5) * 0.5,
	end_s: idx < 5 ? 10.4 + idx * 0.5 : 20.4 + (idx - 5) * 0.5,
	line_final: idx === 4 || idx === 9
})) as LyricWord[];

const index = indexLyricWords(words) as WordIndex;
const at = (t: number, nearS = 0.6) => resolvePointerWord(index, t, { nearS });

const meta = {
	title: 'Contracts/Lyrics/ScrubLyricStrip',
	component: ScrubLyricStrip,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root = typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.position = 'relative';
				root.style.width = '400px';
				root.style.height = '24px';
				root.style.marginTop = '48px';
				root.style.background = 'rgba(128,128,128,0.2)';
			}
			return story();
		}
	],
	args: { index, widthPx: 400, xPx: 200, radius: 2, state: at(10.7) }
} satisfies Meta<typeof ScrubLyricStrip>;
export default meta;
type Story = StoryObj<typeof meta>;

export const InsideWord: Story = contracts.InsideWord;
export const FarGap: Story = contracts.FarGap;
export const BeforeWordRadiusOne: Story = contracts.BeforeWordRadiusOne;
export const NothingToShow: Story = contracts.NothingToShow;

/** Pointer is inside a word: focus word bold in the amber accent. */
export const Inside: Story = {
	args: { state: at(11.1) },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.scrub-readout')).toHaveAttribute('data-kind', 'inside');
	}
};

/** Pointer just before a word: focus word in the cyan accent. */
export const Before: Story = {
	args: { state: at(19.8, 0.6) },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.scrub-readout')).toHaveAttribute('data-kind', 'before');
	}
};

/** Pointer just after a word: focus word in the violet accent. */
export const After: Story = {
	args: { state: at(12.6, 0.6) },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.scrub-readout')).toHaveAttribute('data-kind', 'after');
	}
};

/** Pointer in an instrumental gap far from any word: all dimmed, gap dot shown. */
export const Gap: Story = {
	args: { state: at(16, 0.6) },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.scrub-readout')).toHaveAttribute('data-kind', 'gap');
		await expect(canvasElement.querySelector('.gap-dot')).not.toBeNull();
	}
};

/** xPx near the left edge: the readout clamps inside the strip. */
export const ClampedLeft: Story = {
	args: { state: at(10.1), xPx: -50 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.scrub-readout')).toHaveStyle({ left: '8px' });
	}
};

/** Wider neighbour radius shows more of the line. */
export const WideRadius: Story = {
	args: { state: at(11.1), radius: 4 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.scrub-readout')).toHaveAttribute('data-kind', 'inside');
	}
};
