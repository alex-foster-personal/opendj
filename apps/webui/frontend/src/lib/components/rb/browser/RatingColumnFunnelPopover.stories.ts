import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import RatingColumnFunnelPopover from './RatingColumnFunnelPopover.svelte';
import { EMPTY_RATING_FILTER } from './track-table-column-filters';

/** The Rating column funnel: no constraint, or a minimum of 1 to 5 stars. */
const meta = {
	title: 'Components/Browser/RatingColumnFunnelPopover',
	component: RatingColumnFunnelPopover,
	tags: ['autodocs'],
	args: { filter: EMPTY_RATING_FILTER, onpatch: fn() }
} satisfies Meta<typeof RatingColumnFunnelPopover>;
export default meta;
type Story = StoryObj<typeof meta>;

export const NoFilter: Story = {};
export const ThreeStarsAndUp: Story = { args: { filter: { minStars: 3 } } };
export const FiveStarsOnly: Story = { args: { filter: { minStars: 5 } } };
