import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn } from 'storybook/test';

import { contracts } from './MissingTracksFolder.contract';
import MissingTracksFolder from './MissingTracksFolder.svelte';

const meta = {
	title: 'Contracts/Browser/MissingTracksFolder',
	component: MissingTracksFolder,
	tags: ['autodocs'],
	args: { brokenCount: 42, error: null, selected: false, onselect: fn() }
} satisfies Meta<typeof MissingTracksFolder>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Counted: Story = contracts.Counted;
export const LoadingSelected: Story = contracts.LoadingSelected;
export const Errored: Story = contracts.Errored;

export const Loaded: Story = {
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="playlist-missing-tracks"]')).toHaveTextContent('Missing Tracks');
		await expect(canvasElement.querySelector('.count')).toHaveTextContent('42');
	}
};

/** brokenCount null: the summary has not arrived, count shows an ellipsis. */
export const Loading: Story = {
	args: { brokenCount: null },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.count')).toHaveTextContent('...');
		await expect(canvasElement.querySelector('.count')).toHaveAttribute('title', 'loading broken track count');
	}
};

/** The reserved 'missing' node is the current tree selection. */
export const Selected: Story = {
	args: { selected: true },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="playlist-missing-tracks"]')).toHaveClass('selected');
	}
};

export const ZeroBroken: Story = {
	args: { brokenCount: 0 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.count')).toHaveTextContent('0');
		await expect(canvasElement.querySelector('.count')).toHaveAttribute('title', '0 broken tracks');
	}
};

export const LargeCount: Story = {
	args: { brokenCount: 12408 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.count')).toHaveTextContent('12408');
	}
};
