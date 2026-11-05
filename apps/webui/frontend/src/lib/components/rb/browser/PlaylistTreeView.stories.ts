import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';
import { createRawSnippet } from 'svelte';

import type { PlaylistNode } from '$lib/rb/library-types';

import PlaylistTreeView from './PlaylistTreeView.svelte';

/**
 * The playlist tree panel (SCREENSHOT-SPEC 5b): All Tracks with the live
 * non-broken count, the Playlists folder in rekordbox tree order with counts,
 * deck and multi-pane tints, drop outlines and inline rename, then Recently
 * deleted and Missing Tracks. The container owns drag rules, rename, the
 * CURRENT fold tracker, prefetch and the context menu. ColumnBrowser,
 * Recently deleted and the context menu read the backend themselves, so here
 * they are labelled placeholders. Playlist names and counts are invented.
 */
const playlist = (playlist_id: string, name: string, track_count: number, over: Partial<PlaylistNode> = {}): PlaylistNode => ({
	playlist_id,
	name,
	track_count,
	broken_count: 0,
	kind: 'playlist',
	children: [],
	...over
});

const nodes = [
	playlist('pl-1', 'Warmup Deep', 48),
	playlist('pl-2', 'Peak Time 124', 131, { broken_count: 6 }),
	playlist('pl-3', 'Sunday Closing', 22),
	playlist('pl-4', 'Old Promos', 40, { broken_count: 37, mostly_broken: true }),
	playlist('pl-5', 'A Playlist With A Name Far Too Long For The Tree Column', 9)
];

const placeholder = (label: string) =>
	createRawSnippet(() => ({
		render: () =>
			`<div style="height:20px;line-height:20px;padding:0 6px;color:var(--rb-text-dim);font-style:italic">${label}</div>`
	}));

const meta = {
	title: 'Components/Browser/PlaylistTreeView',
	component: PlaylistTreeView,
	tags: ['autodocs'],
	args: {
		nodes,
		mode: 'tree',
		playlistsLoading: false,
		playlistsError: null,
		hiddenBrokenPlaylistCount: 0,
		allTracksCount: 8355,
		allTracksBrokenCount: 112,
		allTracksError: null,
		allTracksAvailability: null,
		allNode: playlist('all', 'All Tracks', 8355, { kind: 'all_tracks', broken_count: 112 }),
		selectedId: 'all',
		mostlyBrokenTitle: 'Fewer than 5 tracks in this playlist are playable',
		tintOf: () => 'none',
		columnMounted: false,
		dropTargetId: null,
		folderDropActive: false,
		foldCurrent: null,
		selectedRowAction: () => {},
		editingId: null,
		editDraft: '',
		createdId: null,
		cancreate: true,
		canrename: true,
		ondeleteplaylist: fn(),
		onselect: fn(),
		onselectmissing: fn(),
		oncreate: fn(),
		onbeginrename: fn(),
		oneditdraft: fn(),
		onrenamekeydown: fn(),
		onrenamecommit: fn(),
		onmenu: fn(),
		onmenukey: fn(),
		onprefetch: fn(),
		onscroll: fn(),
		onjump: fn(),
		onfolderdragover: fn(),
		onfolderdragleave: fn(),
		onfolderdrop: fn(),
		onrowdragstart: fn(),
		onrowdragover: fn(),
		onrowdragleave: fn(),
		onrowdrop: fn(),
		contextMenu: createRawSnippet(() => ({ render: () => '<span hidden></span>' })),
		columnBrowser: placeholder('Column view (ColumnBrowser)'),
		recentlyDeleted: placeholder('Recently deleted (RecentlyDeletedFolder)')
	},
	parameters: { layout: 'padded' }
} satisfies Meta<typeof PlaylistTreeView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** All Tracks selected; one mostly-broken playlist renders dimmed with its hover title. */
export const Typical: Story = {};

/** A playlist selected: the '+' badge and its row actions show. */
export const PlaylistSelected: Story = { args: { selectedId: 'pl-2' } };

/** Deck-loaded and multi-pane tints on two non-selected rows. */
export const Tints: Story = {
	args: {
		selectedId: 'pl-1',
		tintOf: (node: PlaylistNode) => (node.playlist_id === 'pl-2' ? 'deck' : node.playlist_id === 'pl-3' ? 'multi' : 'none')
	}
};

/** A track drag hovering a playlist, and a Finder folder over the panel. */
export const DropTargets: Story = { args: { dropTargetId: 'pl-3', folderDropActive: true } };

/** A just-created playlist being renamed inline, with the "what next" hint. */
export const NewPlaylistRename: Story = {
	args: {
		nodes: [...nodes, playlist('pl-new', 'New Playlist', 0)],
		selectedId: 'pl-new',
		editingId: 'pl-new',
		editDraft: 'New Playlist',
		createdId: 'pl-new'
	}
};

/** The selected playlist scrolled out of view: the CURRENT fold control shows. */
export const CurrentFold: Story = { args: { selectedId: 'pl-5', foldCurrent: 'below' } };

/** Playlists loading, counts not in yet. */
export const Loading: Story = {
	args: { nodes: [], playlistsLoading: true, allTracksCount: null, allTracksBrokenCount: null, selectedId: null }
};

/** Playlist and count reads failed. */
export const Errors: Story = {
	args: { nodes: [], playlistsError: 'playlists_unavailable', allTracksError: 'counts_unavailable' }
};

/** No playlists and none of the create or rename actions wired. */
export const EmptyReadOnly: Story = {
	args: { nodes: [], cancreate: false, canrename: false, ondeleteplaylist: undefined }
};

/** Column mode: the tree hides and the column view shows. */
export const ColumnMode: Story = { args: { mode: 'column', columnMounted: true } };

/** ARTTHUMB-8728c: the playlist art view option on (variant A tiles and by-lines). */
export const SidebarArt: Story = { args: { sidebarart: true } };
