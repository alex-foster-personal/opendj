import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import { contracts } from './LibraryLoadIndicator.contract';
import LibraryLoadIndicator from './LibraryLoadIndicator.svelte';

const meta = {
	title: 'Contracts/Browser/LibraryLoadIndicator',
	component: LibraryLoadIndicator,
	tags: ['autodocs'],
	args: { loading: false, progress: null, searching: false }
} satisfies Meta<typeof LibraryLoadIndicator>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Determinate: Story = contracts.Determinate;
export const LoadingMoreWhileSearching: Story = contracts.LoadingMoreWhileSearching;
export const SearchingOnly: Story = contracts.SearchingOnly;
export const Idle: Story = contracts.Idle;

/** Load started, first page not back yet: indeterminate. */
export const LoadingNoProgress: Story = {
	args: { loading: true },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByRole('status')).toBeInTheDocument();
		await expect(canvasElement.querySelector('.lli-track')).not.toBeNull();
	}
};

/** Known total: percentage and "N of M rows". */
export const LoadingKnownTotal: Story = {
	args: { loading: true, progress: { loaded: 3200, total: 8400 } },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByText(/3,200 of 8,400 rows/)).toBeInTheDocument();
	}
};

/** Total unknown: only a received-row count, no percentage. */
export const LoadingUnknownTotal: Story = {
	args: { loading: true, progress: { loaded: 1500, total: null } },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByText(/1,500 rows received/)).toBeInTheDocument();
	}
};

/** Loading finished its first pass but more pages are still arriving. */
export const LoadingMore: Story = {
	args: { loading: false, progress: { loaded: 6000, total: 8400 } },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByText(/6,000 of 8,400 rows/)).toBeInTheDocument();
	}
};

/** Search in flight. */
export const Searching: Story = {
	args: { loading: true, searching: true },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByText('searching whole collection...')).toHaveClass('lli-search');
	}
};
