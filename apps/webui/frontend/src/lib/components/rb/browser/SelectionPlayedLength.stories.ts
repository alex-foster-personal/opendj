import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import SelectionPlayedLength from './SelectionPlayedLength.svelte';

const rows = [
	{ order: 1, duration_ms: 300_000, bpm: 120 },
	{ order: 2, duration_ms: 200_000, bpm: null },
	{ order: 3, duration_ms: 100_000, bpm: 120 }
];

const meta = {
	title: 'Contracts/Browser/SelectionPlayedLength',
	component: SelectionPlayedLength,
	tags: ['autodocs'],
	args: { rows, selectedOrders: [1, 2] }
} satisfies Meta<typeof SelectionPlayedLength>;
export default meta;
type Story = StoryObj<typeof meta>;

/** Two rows selected: count, rough mixed length and total. */
export const TwoSelected: Story = {
	play: async ({ canvasElement }) => {
		const readout = canvasElement.querySelector('[data-testid="selection-played-length"]');
		await expect(readout?.textContent).toContain('2 selected · ~6m played · 8m total');
	}
};

/** One row selected: nothing renders. */
export const OneSelected: Story = {
	args: { selectedOrders: [1] },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="selection-played-length"]')).toBeNull();
	}
};
