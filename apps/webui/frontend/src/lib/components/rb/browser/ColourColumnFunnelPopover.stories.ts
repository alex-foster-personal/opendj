import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import ColourColumnFunnelPopover from './ColourColumnFunnelPopover.svelte';
import { EMPTY_COLOUR_FILTER } from './track-table-column-filters';

/** The Colour column funnel: no colour constraint, or a picked set of rekordbox ColorIDs. */
const meta = {
	title: 'Components/Browser/ColourColumnFunnelPopover',
	component: ColourColumnFunnelPopover,
	tags: ['autodocs'],
	args: { filter: EMPTY_COLOUR_FILTER, onpatch: fn() }
} satisfies Meta<typeof ColourColumnFunnelPopover>;
export default meta;
type Story = StoryObj<typeof meta>;

export const NoFilter: Story = {};
export const TwoColoursPicked: Story = { args: { filter: { selected: [1, 4] } } };
