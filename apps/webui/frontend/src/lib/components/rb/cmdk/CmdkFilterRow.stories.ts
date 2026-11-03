import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { LibraryFilterId } from '$lib/rb/library-filter-labels';

import CmdkFilterRow from './CmdkFilterRow.svelte';

/**
 * The library filters as icons under the Cmd K input (CMDK-05). The row is
 * pure props: which filters are on, and a toggle callback.
 */
const states = (on: LibraryFilterId[] = []): Record<LibraryFilterId, boolean> => ({
	broken: on.includes('broken'),
	remixes: on.includes('remixes'),
	vocals: on.includes('vocals'),
	compatible: on.includes('compatible'),
	offline: on.includes('offline')
});

const meta = {
	title: 'Components/Cmdk/CmdkFilterRow',
	component: CmdkFilterRow,
	args: { states: states(), onToggle: fn() }
} satisfies Meta<typeof CmdkFilterRow>;

export default meta;
type Story = StoryObj<typeof meta>;

export const AllOff: Story = {};

export const OneOn: Story = { args: { states: states(['vocals']) } };

export const SeveralOn: Story = { args: { states: states(['broken', 'remixes', 'compatible']) } };

export const AllOn: Story = {
	args: { states: states(['broken', 'remixes', 'vocals', 'compatible', 'offline']) }
};
