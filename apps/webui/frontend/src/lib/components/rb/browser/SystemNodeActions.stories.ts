import type { Meta, StoryObj } from '@storybook/sveltekit';

import SystemNodeActions from './SystemNodeActions.svelte';

/**
 * LIBM-104: Rename and Delete on a protected System tree node, shown dimmed and
 * refused. Both stay focusable (aria-disabled, never `disabled`) so the lock
 * reason in the title and aria-describedby is reachable by keyboard.
 */
const meta = {
	title: 'Components/Browser/SystemNodeActions',
	component: SystemNodeActions,
	tags: ['autodocs']
} satisfies Meta<typeof SystemNodeActions>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The Missing Tracks folder's refused actions. */
export const MissingTracks: Story = {
	args: { name: 'Missing Tracks', nodeId: 'missing' }
};

/** The Recently deleted folder's refused actions. */
export const RecentlyDeleted: Story = {
	args: { name: 'Recently deleted', nodeId: 'recently-deleted' }
};
