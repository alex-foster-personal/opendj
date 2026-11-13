import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import ExplorerPanelView from './ExplorerPanelView.svelte';

/**
 * The Explorer source in the tree slot (issue #9747). Stateless: it renders the
 * folder tree of the collection's stored file paths from props and never fetches.
 * ExplorerSourcePanel.svelte is the wrapper that loads the data. Each story is
 * one state the wrapper can hand it.
 */
const meta = {
	title: 'Components/Browser/ExplorerPanelView',
	component: ExplorerPanelView,
	tags: ['autodocs'],
	// `fn` reports each click in the Actions panel; the wrapper owns the fetches.
	args: {
		tree: {
			roots: [
				{
					name: 'Music',
					path: '/Users/dj/Music',
					track_count: 44,
					direct_track_count: 3,
					children: [
						{
							name: 'House',
							path: '/Users/dj/Music/House',
							track_count: 30,
							direct_track_count: 30,
							children: []
						},
						{
							name: 'Techno',
							path: '/Users/dj/Music/Techno',
							track_count: 11,
							direct_track_count: 11,
							children: []
						}
					]
				}
			],
			track_count: 44,
			unlocated_count: 0
		},
		loading: false,
		error: null,
		expanded: new Set<string>(),
		activePath: null,
		recursive: false,
		oncollection: fn(),
		ontoggle: fn(),
		onopen: fn(),
		onrecursive: fn()
	}
} satisfies Meta<typeof ExplorerPanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The top-level folders, collapsed. */
export const Folders: Story = {};

/** One folder expanded, a child open in the track table, subfolders included. */
export const FolderOpen: Story = {
	args: {
		expanded: new Set(['/Users/dj/Music']),
		activePath: '/Users/dj/Music/House',
		recursive: true
	}
};

/** The first request is still out: the panel says so. */
export const Loading: Story = { args: { tree: null, loading: true } };

/** The folders request failed: the reason is shown, never an empty tree. */
export const FoldersFailed: Story = { args: { tree: null, error: 'daemon returned 500' } };

/** No track has a stored file path yet: the panel says how folders get here. */
export const NoFoldersYet: Story = {
	args: { tree: { roots: [], track_count: 0, unlocated_count: 0 } }
};
