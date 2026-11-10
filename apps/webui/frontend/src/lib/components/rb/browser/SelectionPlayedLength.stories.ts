import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import SelectionPlayedLength from './SelectionPlayedLength.svelte';

const rows = [
	{ order: 1, duration_ms: 300_000 },
	{ order: 2, duration_ms: 200_000 },
	{ order: 3, duration_ms: 100_000 }
];

const meta = {
	title: 'Contracts/Browser/SelectionPlayedLength',
	component: SelectionPlayedLength,
	tags: ['autodocs'],
	args: { visibleRows: rows, rows, selectedOrders: [1, 2] }
} satisfies Meta<typeof SelectionPlayedLength>;
export default meta;
type Story = StoryObj<typeof meta>;

/** Two rows selected: list line plus selection suffix. */
export const TwoSelected: Story = {
	play: async ({ canvasElement }) => {
		const readout = canvasElement.querySelector('[data-testid="library-list-totals"]');
		await expect(readout?.textContent).toContain('selected: 2 tracks, 8:20');
	}
};

/** One row selected: no selection suffix. */
export const OneSelected: Story = {
	args: { selectedOrders: [1] },
	play: async ({ canvasElement }) => {
		const readout = canvasElement.querySelector('[data-testid="library-list-totals"]');
		await expect(readout?.textContent).not.toContain('selected:');
	}
};
