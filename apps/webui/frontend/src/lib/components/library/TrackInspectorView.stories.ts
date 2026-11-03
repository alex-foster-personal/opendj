import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import { summarizeSelection } from '$lib/library/inspector-summary';

import TrackInspectorView from './TrackInspectorView.svelte';

type Props = ComponentProps<typeof TrackInspectorView>;
type Row = NonNullable<Props['single']>;
type Track = NonNullable<Props['track']>;

const row = (over: Partial<Row> = {}): Row => ({
	stable_id: 'a'.repeat(40),
	title: 'Night Drive (Extended Mix)',
	artist: 'Lumen Fields',
	genre: 'Deep House',
	key: '8A',
	bpm: 124,
	rating: 3,
	energy: 6,
	comments: 'warm intro',
	duration_ms: 412_000,
	artwork_available: false,
	...over
});

const track: Track = {
	stable_id: 'a'.repeat(40),
	title: 'Night Drive (Extended Mix)',
	artist: 'Lumen Fields',
	rating: 3,
	notes: 'Into the second breakdown, not the first.',
	tags: ['peak', 'vocal'],
	play_count: 4,
	artwork_available: false,
	auto_cues_available: false,
	has_rb_mapping: true,
	lyrics_available: false,
	stems_available: false,
	created_at: '2026-09-01T10:00:00Z',
	updated_at: '2026-10-12T21:30:00Z',
	provenance: {
		bpm: { source: 'own_beatgrid', modified_at: '2026-10-02T09:00:00Z', confidence: 0.97, status: 'ok', value: 124 },
		key: { source: 'mik', modified_at: '2026-09-20T09:00:00Z', status: 'ok', value: '8A' },
		rating: { source: 'user', modified_at: '2026-10-12T21:30:00Z', status: 'ok', value: 3 }
	}
};

/**
 * The Crate Desk inspector (LIBWS-02): one track's fields with who set each
 * one, its tags, notes and playlists; or a summary when several are
 * selected. The container loads the track and owns the save queue; edits
 * raise callbacks. Tracks are invented and have no artwork, so nothing is
 * fetched.
 */
const meta = {
	title: 'Components/Library/TrackInspectorView',
	component: TrackInspectorView,
	tags: ['autodocs'],
	args: {
		rowCount: 1,
		paneTitle: 'Warehouse Warmups',
		summary: null,
		single: row(),
		track,
		loadError: null,
		saveError: null,
		playlists: [
			{ playlist_id: 'pl-1', name: 'Warehouse Warmups', vendor: 'rekordbox', positions: [3] },
			{ playlist_id: 'pl-2', name: 'Deep Cuts', vendor: 'rekordbox', positions: [17, 42] }
		],
		playlistsError: null,
		artFailed: false,
		newTag: '',
		applyPatch: fn(async () => {}),
		addTag: fn(),
		onArtError: fn(),
		onTempoPref: fn()
	}
} satisfies Meta<typeof TrackInspectorView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** One track loaded, with source chips, tags and playlists. */
export const SingleTrack: Story = {};

/** The track's saved version is still loading: stars, tags and notes are inert. */
export const Loading: Story = { args: { track: null, playlists: null } };

/** The track read and the playlist read both failed. */
export const LoadFailed: Story = {
	args: { track: null, loadError: 'Error: 404 Not Found', playlists: null, playlistsError: 'Error: 503' }
};

/** The last save failed. */
export const SaveFailed: Story = { args: { saveError: 'Error: 500 Internal Server Error' } };

/** In no playlist. */
export const NoPlaylists: Story = { args: { playlists: [] } };

/** Several tracks selected: a field the rows disagree on reads Multiple values. */
export const MultiSelect: Story = {
	args: {
		rowCount: 3,
		single: null,
		track: null,
		summary: summarizeSelection([
			row(),
			row({ stable_id: 'b'.repeat(40), artist: 'Quiet Atlas', key: '9A', bpm: 126, duration_ms: null }),
			row({ stable_id: 'c'.repeat(40), key: '8B', bpm: 122, rating: 5 })
		])
	}
};

/** Nothing selected. */
export const Empty: Story = { args: { rowCount: 0, single: null, track: null } };
