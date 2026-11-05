import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import PaneTabs, { type PaneTabInfo } from './PaneTabs.svelte';

const tab = (over: Partial<PaneTabInfo>): PaneTabInfo => ({
	title: 'blank list',
	count: null,
	loading: false,
	sticky: false,
	ephemeral: false,
	...over
});

const meta = {
	title: 'Components/Browser/PaneTabs',
	component: PaneTabs,
	tags: ['autodocs'],
	args: {
		active: 0,
		onactivate: fn(),
		ontogglesticky: fn(),
		onreorder: fn(),
		ondropplaylist: fn(),
		onsaveas: fn(),
		onaddpane: fn()
	}
} satisfies Meta<typeof PaneTabs>;

export default meta;
type Story = StoryObj<typeof meta>;

/** One pane showing a loaded playlist; room to add more panes. */
export const SinglePane: Story = {
	args: { tabs: [tab({ title: 'Warehouse Warmups', count: 128 })] }
};

/** Four panes, the maximum: the add-pane control is not offered. */
export const FourPanes: Story = {
	args: {
		active: 1,
		tabs: [
			tab({ title: 'Warehouse Warmups', count: 128 }),
			tab({ title: 'Sunrise Closers', count: 54 }),
			tab({ title: 'blank list' }),
			tab({ title: 'Deep Cuts', count: 311 })
		]
	}
};

/** The active pane is hydrating its playlist. */
export const Loading: Story = {
	args: { tabs: [tab({ title: 'Warehouse Warmups', loading: true }), tab({ title: 'blank list' })] }
};

/** Sticky (locked) tab: playlist selections route to another pane. */
export const StickyTab: Story = {
	args: {
		tabs: [
			tab({ title: 'Warehouse Warmups', count: 128, sticky: true }),
			tab({ title: 'blank list' })
		]
	}
};

/** Ephemeral pane holding rows with no playlist: offers Save as. */
export const EphemeralSaveAs: Story = {
	args: { tabs: [tab({ title: 'blank list', count: 17, ephemeral: true })] }
};

export const LongTitle: Story = {
	args: {
		tabs: [
			tab({
				title: 'Late Night Peak Hour Rolling Basslines and Other Slow Burners (Archive 2019)',
				count: 1042
			})
		]
	}
};
