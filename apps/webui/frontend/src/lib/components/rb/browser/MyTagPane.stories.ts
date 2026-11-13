import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import MyTagPane from './MyTagPane.svelte';

/**
 * The My Tag pane docked right of the track table (issue #10026). Stateless: it
 * renders the tag catalog grouped by tag type as checkboxes bound to the
 * selection, from props; BrowserPanel owns the write. Each story is one state
 * BrowserPanel can hand it.
 */
const meta = {
	title: 'Components/Browser/MyTagPane',
	component: MyTagPane,
	tags: ['autodocs'],
	// `fn` reports each tick in the Actions panel; BrowserPanel owns the write.
	args: {
		groups: [
			{
				id: 'genre',
				label: 'Genre',
				tags: [
					{ name: 'House', color: '#e0a030', state: 'checked', held: 1 },
					{ name: 'Techno', color: null, state: 'clear', held: 0 }
				]
			},
			{
				id: 'other',
				label: 'Other',
				tags: [{ name: 'Warm up', color: '#3080e0', state: 'clear', held: 0 }]
			}
		],
		selectedCount: 1,
		busy: false,
		ontoggle: fn()
	}
} satisfies Meta<typeof MyTagPane>;

export default meta;
type Story = StoryObj<typeof meta>;

/** One track selected: each box shows whether the track holds the tag. */
export const OneTrack: Story = {};

/** Several tracks selected: a tag only some of them hold shows as mixed. */
export const SeveralTracks: Story = {
	args: {
		selectedCount: 3,
		groups: [
			{
				id: 'genre',
				label: 'Genre',
				tags: [
					{ name: 'House', color: '#e0a030', state: 'mixed', held: 2 },
					{ name: 'Techno', color: null, state: 'checked', held: 3 }
				]
			}
		]
	}
};

/** Nothing selected: the pane says what to do. */
export const NothingSelected: Story = { args: { selectedCount: 0 } };

/** No tags in the catalog yet: the pane says where tags come from. */
export const NoTagsYet: Story = { args: { groups: [] } };

/** A write is in flight: the boxes wait for it. */
export const Busy: Story = { args: { busy: true } };
