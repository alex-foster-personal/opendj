import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import RecentlyDeletedFolderView from './RecentlyDeletedFolderView.svelte';

/**
 * The tree's "Recently deleted" folder (LIBMX-03). The container owns the
 * deleted-playlist list and the restore call; this draws the folder row, its
 * count (with the hover title) and, when open, one row per deleted playlist
 * with a restore button. Playlist names are invented.
 */
const meta = {
	title: 'Components/Browser/RecentlyDeletedFolderView',
	component: RecentlyDeletedFolderView,
	tags: ['autodocs'],
	args: { open: false, rows: [], error: null, ontoggle: fn(), onrestore: fn() },
	parameters: { layout: 'padded' }
} satisfies Meta<typeof RecentlyDeletedFolderView>;

export default meta;
type Story = StoryObj<typeof meta>;

const deleted = (playlist_id: string, name: string, track_count: number) => ({
	playlist_id,
	name,
	track_count,
	deleted_at: '2026-10-12T21:04:00Z',
	updated_at: '2026-10-12T21:04:00Z',
	vendor: 'rekordbox',
	vendor_pl_id: playlist_id
});

const rows = [deleted('pl-7', 'Old Warmup', 32), deleted('pl-9', 'Test Crate', 4)];

/** Collapsed: just the folder row and its count. */
export const Collapsed: Story = { args: { rows } };

/** Open with two deleted playlists, each restorable. */
export const Open: Story = { args: { open: true, rows } };

/** Open and nothing has been deleted. */
export const OpenEmpty: Story = { args: { open: true } };

/** The list has not loaded yet: the count reads '...'. */
export const Loading: Story = { args: { open: true, rows: null } };

/** The list failed: '!' count with the reason in its hover title. */
export const Error: Story = { args: { open: true, rows: null, error: 'playlists_unavailable' } };
