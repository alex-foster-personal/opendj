import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { ComponentProps } from 'svelte';

import SpotifySourcePanelView from './SpotifySourcePanelView.svelte';

/**
 * The Spotify source: imported Spotify playlists (pinned first, then
 * recent, with a filter) above the selected playlist's buy list of tracks
 * not in the library, plus (SPOT-6555) every playlist on the signed-in account
 * and the side player controls. The container reads prefs, talks to the engine
 * and the shell, and owns the pin toggle. Titles, artists and links are invented.
 */
type Props = ComponentProps<typeof SpotifySourcePanelView>;
type Playlist = Props['playlists'][number];
type Pending = NonNullable<Props['pendingTracks']>[number];

const playlist = (playlist_id: string, name: string, track_count: number): Playlist => ({
	playlist_id,
	name,
	track_count,
	available_count: track_count,
	forbid_duplicates: false,
	updated_at: '2026-10-10T12:00:00Z',
	vendor: 'spotify'
});

const pending = (pending_id: number, title: string, artist: string, album: string | null): Pending => ({
	pending_id,
	playlist_id: 'sp-1',
	position: pending_id,
	spotify_uri: `spotify:track:example${pending_id}`,
	isrc: null,
	title,
	artist,
	album,
	duration_ms: 381000,
	suggested_sources: {
		beatport: 'https://example.com/beatport',
		bandcamp: 'https://example.com/bandcamp',
		qobuz: 'https://example.com/qobuz',
		apple_music: 'https://example.com/apple-music',
		discogs: 'https://example.com/discogs'
	},
	status: 'pending',
	added_at: '2026-10-10T12:00:00Z'
});

const playlists = [
	playlist('sp-1', 'Discover Weekly Picks', 30),
	playlist('sp-2', 'Late Night Deep', 112),
	playlist('sp-3', 'Festival Warmup', 54)
];

// An account playlist as the engine lists it; the player URL is built from the id.
const accountPlaylist = (spotify_id: string, name: string, track_count: number) => ({
	spotify_id,
	name,
	owner_id: 'invented-dj',
	owner_name: null,
	owned: true,
	collaborative: false,
	public: null,
	track_count,
	image_url: null,
	open_url: `https://open.spotify.com/playlist/${spotify_id}`,
	snapshot_id: null
});

const signedIn = {
	connected: true as const,
	user_id: 'invented-dj',
	display_name: 'Invented DJ',
	total_reported: 2,
	playlists: [accountPlaylist('0InventedPlaylistA', 'Sunrise Set Ideas', 21), accountPlaylist('0InventedPlaylistB', 'Warehouse Openers', 40)]
};

const meta = {
	title: 'Components/Browser/SpotifySourcePanelView',
	component: SpotifySourcePanelView,
	tags: ['autodocs'],
	args: {
		playlists,
		playlistsLoading: false,
		playlistsError: null,
		selectedId: null,
		pendingTracks: null,
		loading: false,
		error: null,
		pinnedIds: ['sp-2'],
		recentIds: [],
		purchaseSources: ['beatport', 'bandcamp', 'qobuz', 'apple_music', 'discogs'],
		oncollection: fn(),
		onselect: fn(),
		ontogglepin: fn(),
		account: null,
		accountLoading: false,
		accountError: null,
		connectNote: null,
		playerNote: null,
		connectHere: true,
		nativeShell: false,
		sidePlayerDocked: false,
		shrinkMain: true,
		clientIdSaving: false,
		onrefresh: fn(),
		onconnect: fn(),
		onplay: fn(),
		onimportlogins: fn(),
		onshrinkchange: fn(),
		onsaveclientid: fn(async () => true),
		onopenexternal: fn()
	},
	parameters: { layout: 'fullscreen' }
} satisfies Meta<typeof SpotifySourcePanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Playlists listed (one pinned), none chosen yet. */
export const NoneSelected: Story = {};

/** A playlist chosen: its buy list with purchase links. */
export const BuyList: Story = {
	args: {
		selectedId: 'sp-1',
		pendingTracks: [
			pending(1, 'Night Drive (Extended Mix)', 'Lumen Fields', 'Night Drive EP'),
			pending(2, 'Glass Harbor', 'Example Artist', null)
		]
	}
};

/** Everything on the chosen playlist is already in the library. */
export const BuyListEmpty: Story = { args: { selectedId: 'sp-1', pendingTracks: [] } };

/** The buy list is loading. */
export const BuyListLoading: Story = { args: { selectedId: 'sp-1', loading: true } };

/** The buy list failed. */
export const BuyListError: Story = { args: { selectedId: 'sp-1', error: 'spotify_pending_unavailable' } };

/** The imported playlists are loading (no filter shown). */
export const PlaylistsLoading: Story = { args: { playlists: [], playlistsLoading: true } };

/** No Spotify playlist was ever imported. */
export const NoPlaylists: Story = { args: { playlists: [] } };

/** The playlist load failed. */
export const PlaylistsError: Story = { args: { playlists: [], playlistsError: 'spotify_not_connected' } };

/** SPOT-6555: a signed-in account lists its own playlists beside the imported ones. */
export const AccountSignedIn: Story = { args: { account: signedIn, sidePlayerDocked: true } };

/** SPOT-6555: the account's rows stay listed while the imported list is still loading. */
export const AccountWhileImportsLoad: Story = { args: { account: signedIn, playlistsLoading: true } };

/** SPOT-6555: signed out, the panel says why and offers Connect Spotify. */
export const AccountSignedOut: Story = {
	args: { account: { connected: false, reason: 'not_signed_in', message: 'Sign in to Spotify.', playlists: [] } }
};

/** SPOT-6555: a fresh install asks for the user's own Spotify app Client ID first. */
export const AccountNoClientId: Story = {
	args: { account: { connected: false, reason: 'no_client_id', message: 'No Spotify Client ID.', playlists: [] } }
};
