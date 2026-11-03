import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import TrackPlaylistsPopoverView from './TrackPlaylistsPopoverView.svelte';

/**
 * "Show in playlists" from a track's context menu: every playlist holding the
 * track. The container fetches the hits and opens the picked playlist in the
 * active pane; this places itself at the click point, takes focus, and closes
 * on Escape or an outside press. Playlist names are invented.
 */
const meta = {
	title: 'Components/Browser/TrackPlaylistsPopoverView',
	component: TrackPlaylistsPopoverView,
	tags: ['autodocs'],
	args: {
		x: 60,
		y: 40,
		loading: false,
		errorMessage: null,
		hits: [],
		onselect: fn(),
		onclose: fn()
	}
} satisfies Meta<typeof TrackPlaylistsPopoverView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The track sits in three playlists. */
export const InPlaylists: Story = {
	args: {
		hits: [
			{ playlist_id: 'pl-1', name: 'Warmup Deep', vendor: 'rekordbox', positions: [3] },
			{ playlist_id: 'pl-2', name: 'Peak Time 124', vendor: 'rekordbox', positions: [11, 40] },
			{ playlist_id: 'pl-3', name: 'Sunday Closing', vendor: 'rekordbox', positions: [1] }
		]
	}
};

/** The lookup is in flight: one disabled loading row. */
export const Loading: Story = { args: { loading: true } };

/** The track is in no playlist. */
export const NotInAny: Story = {};

/** The lookup failed; the reason replaces the list. */
export const Error: Story = { args: { errorMessage: 'Track not found' } };
