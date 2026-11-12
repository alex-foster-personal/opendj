import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import PlaylistSummaryLine from './PlaylistSummaryLine.svelte';

/**
 * Aggregate stats row under the playlist header (SETPREP-10645): track count,
 * duration, BPM spread, energy spread, key count, unanalysed tally.
 */
const sampleStats = {
	tracks: 42,
	duration_s: 10_080,
	bpm: { min: 122, median: 126, max: 128, missing: 1 },
	energy: { min: 5, max: 8, missing: 2 },
	keys: { distinct: 9, missing: 1 },
	unanalysed: 3
};

const meta = {
	title: 'Components/Browser/PlaylistSummaryLine',
	component: PlaylistSummaryLine,
	tags: ['autodocs'],
	args: { stats: sampleStats }
} satisfies Meta<typeof PlaylistSummaryLine>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Typical playlist: one line matches the acceptance example shape. */
export const Default: Story = {
	play: async ({ canvasElement }) => {
		const line = within(canvasElement).getByTestId('playlist-summary-line');
		await expect(line.textContent).toMatch(/^42 tracks, 2 h 48 m,/);
		await expect(line.textContent).toMatch(/3 unanalysed$/);
		const title = line.getAttribute('title') ?? '';
		await expect(title).toContain('42 tracks');
	}
};

/** No stats yet: the header omits the line entirely. */
export const NoStats: Story = {
	tags: ['renders-nothing'],
	args: { stats: null },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="playlist-summary-line"]')).toBeNull();
	}
};

export const EmptyPlaylist: Story = {
	args: {
		stats: {
			tracks: 0,
			duration_s: 0,
			bpm: { min: null, median: null, max: null, missing: 0 },
			energy: { min: null, max: null, missing: 0 },
			keys: { distinct: 0, missing: 0 },
			unanalysed: 0
		}
	}
};
