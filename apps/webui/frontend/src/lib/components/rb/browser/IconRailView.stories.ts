import type { Meta, StoryObj } from '@storybook/sveltekit';

/**
 * Tombstone: IconRailView was merged back into IconRail.svelte (issue #9451
 * Storybook presentational contract). This story only satisfies the coverage
 * guard for the leftover file; see Components/Browser/IconRail for the rail UI.
 */
import IconRailView from './IconRailView.svelte';

const meta = {
	title: 'Components/Browser/IconRailView',
	component: IconRailView,
	tags: ['autodocs']
} satisfies Meta<typeof IconRailView>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Superseded: Story = {};
