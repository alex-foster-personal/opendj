import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import TrackFilterBar from './TrackFilterBar.svelte';
import { EMPTY_TRACK_FILTER_STATE } from './track-filter-state';

const BPM_AND_KEYS = {
	...EMPTY_TRACK_FILTER_STATE,
	bpm: { min: 120, max: 128 },
	key: { selected: ['8A', '9A'] }
};

/** The Track Filter bar: one chip per active column funnel, a suspend switch and Clear all. */
const meta = {
	title: 'Components/Browser/TrackFilterBar',
	component: TrackFilterBar,
	tags: ['autodocs'],
	args: {
		filterState: BPM_AND_KEYS,
		shown: 42,
		total: 310,
		onsuspend: fn(),
		onclear: fn(),
		onopen: fn()
	}
} satisfies Meta<typeof TrackFilterBar>;
export default meta;
type Story = StoryObj<typeof meta>;

export const TwoActiveFilters: Story = {};
export const Suspended: Story = {
	args: { filterState: { ...BPM_AND_KEYS, suspended: true }, shown: 310 }
};
