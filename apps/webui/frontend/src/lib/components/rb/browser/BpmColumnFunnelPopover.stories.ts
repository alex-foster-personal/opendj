import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import BpmColumnFunnelPopover from './BpmColumnFunnelPopover.svelte';
import { EMPTY_BPM_FILTER } from './track-table-column-filters';

/** The BPM column funnel: no BPM constraint, or an inclusive min/max BPM range. */
const meta = {
	title: 'Components/Browser/BpmColumnFunnelPopover',
	component: BpmColumnFunnelPopover,
	tags: ['autodocs'],
	args: { filter: EMPTY_BPM_FILTER, onpatch: fn() }
} satisfies Meta<typeof BpmColumnFunnelPopover>;
export default meta;
type Story = StoryObj<typeof meta>;

export const NoFilter: Story = {};
export const RangeSet: Story = { args: { filter: { min: 120, max: 128 } } };
export const MinOnly: Story = { args: { filter: { min: 140, max: null } } };
