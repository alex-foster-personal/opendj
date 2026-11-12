import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import { contracts } from './RecentlyDeletedFolderView.contract';
import RecentlyDeletedFolderView from './RecentlyDeletedFolderView.svelte';

/**
 * The tree's "Recently deleted" folder (LIBMX-03). The container owns the
 * deleted-playlist list and the restore call; this draws the folder row, its
 * count (with the hover title) and, when open, one row per deleted playlist
 * with a restore button. Playlist names are invented.
 */
const meta = {
	title: 'Contracts/Browser/RecentlyDeletedFolderView',
	component: RecentlyDeletedFolderView,
	tags: ['autodocs'],
	args: { open: false, rows: [], error: null, ontoggle: fn(), onrestore: fn() },
	parameters: { layout: 'padded' }
} satisfies Meta<typeof RecentlyDeletedFolderView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Collapsed: just the folder row and its count. */
export const Collapsed: Story = contracts.Collapsed;

/** Open with two deleted playlists, each restorable. */
export const Open: Story = contracts.OpenWithRows;

/** Open and nothing has been deleted. */
export const OpenEmpty: Story = contracts.EmptyAfterSuccess;

/** The list has not loaded yet: the count reads '...'. */
export const Loading: Story = contracts.Loading;

/** The list failed: '!' count with the reason in its hover title. */
export const Error: Story = contracts.Error;

/** Successful load with zero rows after a prior failure would clear error. */
export const EmptyAfterSuccess: Story = contracts.EmptyAfterSuccess;
