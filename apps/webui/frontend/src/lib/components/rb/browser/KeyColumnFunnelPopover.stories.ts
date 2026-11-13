import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import KeyColumnFunnelPopover from './KeyColumnFunnelPopover.svelte';
import { EMPTY_KEY_FILTER } from './track-table-column-filters';

/** The Key column funnel: no key constraint, or a picked set of canonical Camelot keys. */
const meta = {
	title: 'Components/Browser/KeyColumnFunnelPopover',
	component: KeyColumnFunnelPopover,
	tags: ['autodocs'],
	args: { filter: EMPTY_KEY_FILTER, onpatch: fn() }
} satisfies Meta<typeof KeyColumnFunnelPopover>;
export default meta;
type Story = StoryObj<typeof meta>;

export const NoFilter: Story = {};
export const TwoKeysPicked: Story = { args: { filter: { selected: ['8A', '9B'] } } };
