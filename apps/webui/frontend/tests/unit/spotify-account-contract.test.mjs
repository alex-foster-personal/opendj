/**
 * SPOT-6555: the panel checks the engine's account listing field by field, so a
 * one-sided backend change breaks loudly instead of rendering invented rows.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/spotify-account-api.ts');
});

const row = (extra = {}) => ({
	spotify_id: 'aaa',
	name: 'Invented aaa',
	owner_id: 'me',
	owner_name: null,
	owned: true,
	collaborative: false,
	public: null,
	track_count: 3,
	image_url: null,
	open_url: 'https://open.spotify.com/playlist/aaa',
	snapshot_id: null,
	...extra
});
const listing = (extra = {}) => ({
	connected: true,
	user_id: 'me',
	display_name: null,
	total_reported: 1,
	playlists: [row()],
	...extra
});

test('a well-formed listing parses to the same rows', () => {
	assert.deepEqual(mod.parseSpotifyAccountListing(listing()), listing());
	assert.equal(mod.parseSpotifyAccountListing(listing({ total_reported: 0, playlists: [] })).total_reported, 0);
	const off = { connected: false, reason: 'not_signed_in', message: 'Sign in', playlists: [] };
	assert.deepEqual(mod.parseSpotifyAccountListing(off), off);
});

for (const [label, body, field] of [
	['missing total_reported', (() => { const b = listing(); delete b.total_reported; return b; })(), 'total_reported'],
	['string total_reported', listing({ total_reported: '1' }), 'total_reported'],
	// A connected listing must prove it is complete.
	['null total_reported', listing({ total_reported: null }), 'total_reported'],
	['empty user_id', listing({ user_id: '' }), 'user_id'],
	['empty spotify_id', listing({ playlists: [row({ spotify_id: '' })] }), 'playlists[0].spotify_id'],
	['null owner_id', listing({ playlists: [row({ owner_id: null })] }), 'playlists[0].owner_id'],
	['empty owner_id', listing({ playlists: [row({ owner_id: '' })] }), 'playlists[0].owner_id'],
	['missing display_name', (() => { const b = listing(); delete b.display_name; return b; })(), 'display_name'],
	['row without a name', listing({ playlists: [row({ name: undefined })] }), 'playlists[0].name'],
	['truthy collaborative', listing({ playlists: [row({ collaborative: 'yes' })] }), 'playlists[0].collaborative'],
	['missing nullable field', listing({ playlists: [row({ snapshot_id: undefined })] }), 'playlists[0].snapshot_id'],
	['fractional track count', listing({ playlists: [row({ track_count: 2.5 })] }), 'playlists[0].track_count'],
	['row that is not an object', listing({ playlists: [null] }), 'playlists[0]'],
	// The id is base-62 and the URL is the one built from it.
	['uri spotify_id', listing({ playlists: [row({ spotify_id: 'spotify:playlist:aaa', open_url: 'https://open.spotify.com/playlist/spotify:playlist:aaa' })] }), 'playlists[0].spotify_id'],
	["another playlist's open_url", listing({ playlists: [row({ open_url: 'https://open.spotify.com/playlist/bbb' })] }), 'playlists[0].open_url'],
	['album open_url', listing({ playlists: [row({ open_url: 'https://open.spotify.com/album/aaa' })] }), 'playlists[0].open_url'],
	['numeric open_url', listing({ playlists: [row({ open_url: 7 })] }), 'playlists[0].open_url'],
	['unknown reason', { connected: false, reason: 'banned', message: 'x', playlists: [] }, 'reason'],
	// Disconnected still carries playlists, and it is empty.
	['disconnected without playlists', { connected: false, reason: 'not_signed_in', message: 'x' }, 'playlists'],
	['disconnected with playlists', { connected: false, reason: 'not_signed_in', message: 'x', playlists: [row()] }, 'playlists']
]) {
	test(`${label} is a contract violation`, () => {
		assert.throws(() => mod.parseSpotifyAccountListing(body), new RegExp(`contract violation: ${field.replace(/[[\]]/g, '\\$&')}$`));
	});
}
