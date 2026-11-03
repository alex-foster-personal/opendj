import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import { contracts } from './LyricVerdictMark.contract';
import LyricVerdictMark from './LyricVerdictMark.svelte';
import { LYRIC_UNCHECKED_MARK, LYRIC_VERDICT_MARKS } from './lyric-column';

const meta = {
	title: 'Contracts/Browser/LyricVerdictMark',
	component: LyricVerdictMark,
	tags: ['autodocs'],
	args: { mark: LYRIC_VERDICT_MARKS.vocal }
} satisfies Meta<typeof LyricVerdictMark>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Vocal: Story = contracts.Vocal;
export const Pending: Story = contracts.Pending;
export const Sparse: Story = contracts.Sparse;

/** Lyrics found but not yet checked: lyric sheet with a pending dot. */
export const Unchecked: Story = {
	args: { mark: LYRIC_UNCHECKED_MARK },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('svg.lyr-mark-pending')).not.toBeNull();
		await expect(canvasElement.querySelector('.lyr-pending-dot')).not.toBeNull();
	}
};

export const NoLyrics: Story = {
	args: { mark: LYRIC_VERDICT_MARKS['no-lyrics'] },
	play: async ({ canvasElement }) => {
		await expect(canvasElement).toHaveTextContent('∅');
	}
};

export const Unknown: Story = {
	args: { mark: LYRIC_VERDICT_MARKS.unknown },
	play: async ({ canvasElement }) => {
		await expect(canvasElement).toHaveTextContent('?');
	}
};
