/** SPOT-6555: merging the live account listing with imported playlists. */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let rank;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/spotify-account-merge.ts');
	rank = await loadTypeScriptModule('src/lib/rb/spotify-playlist-rank.ts');
});

const acct = (id, extra = {}) => ({
	spotify_id: id,
	name: `Invented ${id}`,
	owner_id: 'me',
	owner_name: null,
	owned: true,
	collaborative: false,
	public: null,
	track_count: 3,
	image_url: null,
	open_url: `https://open.spotify.com/playlist/${id}`,
	snapshot_id: null,
	...extra
});
const imp = (pid, vendor, extra = {}) => ({
	playlist_id: pid,
	name: `Imported ${pid}`,
	track_count: 7,
	vendor_pl_id: vendor,
	...extra
});

test('every account playlist becomes a row; an imported one keeps its state.db id', () => {
	const rows = mod.mergeSpotifyPlaylists([acct('aaa'), acct('bbb')], [imp('spotify:bbb', 'bbb')]);
	assert.deepEqual(
		rows.map((r) => r.playlist_id),
		['spotify-account:aaa', 'spotify:bbb']
	);
	assert.equal(rows[1].imported.playlist_id, 'spotify:bbb');
	assert.equal(rows[0].imported, null);
	assert.equal(rows[0].open_url, 'https://open.spotify.com/playlist/aaa');
	assert.deepEqual(
		rows.map((r) => r.pref_key),
		['spotify:aaa', 'spotify:bbb']
	);
});

test('imported playlists the account does not list stay, after the account rows', () => {
	const rows = mod.mergeSpotifyPlaylists([acct('aaa')], [imp('spotify:zzz', 'zzz')]);
	assert.deepEqual(
		rows.map((r) => r.playlist_id),
		['spotify-account:aaa', 'spotify:zzz']
	);
	assert.equal(rows[1].account, null);
	assert.equal(rows[1].open_url, 'https://open.spotify.com/playlist/zzz');
});

test('the imported tally counts only the account playlists, never one the account dropped', () => {
	const rows = mod.mergeSpotifyPlaylists(
		[acct('aaa'), acct('bbb')],
		[imp('spotify:bbb', 'bbb'), imp('spotify:zzz', 'zzz'), imp('spotify:yyy', 'yyy')]
	);
	assert.equal(rows.length, 4);
	// Three imported rows, but only one is the account's: "1 of 2", never "3 of 2".
	assert.equal(mod.importedAccountCount(rows), 1);
	// Control: every account playlist imported counts them all.
	assert.equal(
		mod.importedAccountCount(mod.mergeSpotifyPlaylists([acct('aaa')], [imp('spotify:aaa', 'aaa')])),
		1
	);
	assert.equal(
		mod.importedAccountCount(mod.mergeSpotifyPlaylists(null, [imp('spotify:zzz', 'zzz')])),
		0
	);
});

test('not connected: the imported list alone, unchanged', () => {
	const rows = mod.mergeSpotifyPlaylists(null, [
		imp('spotify:one', 'one'),
		imp('spotify:two', 'two')
	]);
	assert.equal(rows.length, 2);
	assert.ok(rows.every((r) => r.imported !== null));
});

test('a missing vendor_pl_id falls back to the id tail; an unparseable id has no player URL', () => {
	assert.equal(mod.importedSpotifyId(imp('spotify:abc123', null)), 'abc123');
	assert.equal(mod.importedSpotifyId(imp('spotify:playlist:abc123', null)), 'abc123');
	assert.equal(mod.importedSpotifyId(imp('spotify:user:dj:playlist:abc123', null)), 'abc123');
	const rows = mod.mergeSpotifyPlaylists(null, [imp('weird/id', null)]);
	assert.equal(rows[0].open_url, null);
});

test('a vendor_pl_id the side player would refuse gives no player URL', () => {
	for (const vendor of [
		'spotify:playlist:abc123',
		'https://open.spotify.com/playlist/abc123',
		'abc 123',
		'abc/123'
	]) {
		assert.equal(mod.importedSpotifyId(imp('spotify:abc123', vendor)), null, vendor);
		const rows = mod.mergeSpotifyPlaylists(null, [imp('spotify:abc123', vendor)]);
		assert.equal(rows[0].open_url, null, vendor);
	}
	// Control: a base-62 vendor id still wins over the id tail.
	assert.equal(mod.importedSpotifyId(imp('spotify:abc123', 'Zz09')), 'Zz09');
	assert.equal(
		mod.mergeSpotifyPlaylists(null, [imp('spotify:abc123', 'Zz09')])[0].open_url,
		'https://open.spotify.com/playlist/Zz09'
	);
});

test('#9685: a pinned account-only row stays pinned and keeps its recent rank once imported', () => {
	const before = mod.mergeSpotifyPlaylists([acct('aaa'), acct('bbb'), acct('ccc')], []);
	const pinned = rank.togglePinnedId([], before[1].pref_key);
	const recent = rank.prependRecentId(
		rank.prependRecentId([], before[2].pref_key),
		before[0].pref_key
	);
	assert.equal(before[1].pref_key, 'spotify:bbb');
	assert.equal(before[1].playlist_id, 'spotify-account:bbb');
	const after = mod.mergeSpotifyPlaylists(
		[acct('aaa'), acct('bbb'), acct('ccc')],
		[imp('spotify:bbb', undefined), imp('spotify:ccc', 'ccc')]
	);
	assert.equal(after[1].playlist_id, 'spotify:bbb');
	assert.equal(after[1].pref_key, before[1].pref_key);
	const rankArgs = { query: '', pinnedIds: pinned, recentIds: recent };
	const order = (rows) => rank.rankSpotifyPlaylists(rows, rankArgs).map((r) => r.pref_key);
	assert.deepEqual(order(before), ['spotify:bbb', 'spotify:aaa', 'spotify:ccc']);
	assert.deepEqual(order(after), ['spotify:bbb', 'spotify:aaa', 'spotify:ccc']);
});

test('pref_key falls back to the state.db id only when no Spotify id is known', () => {
	const rows = mod.mergeSpotifyPlaylists(null, [
		imp('local-pl-1', undefined),
		imp('spotify:zzz', 'not a valid id')
	]);
	assert.equal(rows[0].pref_key, 'local-pl-1');
	assert.equal(rows[0].open_url, null);
	assert.equal(rows[1].pref_key, 'spotify:zzz');
	assert.equal(rows[1].open_url, null);
});

test('spotifyPreferenceKey maps old forms and leaves the rest byte-identical', () => {
	const cases = [
		['spotify-account:aaa', 'spotify:aaa'],
		['spotify:aaa', 'spotify:aaa'],
		['spotify:playlist:aaa', 'spotify:aaa'],
		['spotify:user:me:playlist:aaa', 'spotify:aaa'],
		['pl-1', 'pl-1'],
		['spotify-account:not base62!', 'spotify-account:not base62!'],
		['spotify:a:b:c', 'spotify:a:b:c']
	];
	for (const [input, expected] of cases) {
		assert.equal(rank.spotifyPreferenceKey(input), expected, input);
	}
});

test('playSpotifyRow records the row only after the player opened', async () => {
	const row = {
		open_url: 'https://open.spotify.com/playlist/aaa',
		pref_key: 'spotify:aaa'
	};
	let openUrl = null;
	const rememberCalls = [];
	const open = async (url) => {
		openUrl = url;
	};
	const remember = (key) => {
		rememberCalls.push(key);
	};
	await mod.playSpotifyRow(row, open, remember);
	assert.equal(openUrl, row.open_url);
	assert.deepEqual(rememberCalls, ['spotify:aaa']);

	rememberCalls.length = 0;
	await assert.rejects(
		async () =>
			mod.playSpotifyRow(
				row,
				async () => {
					throw new Error('blocked');
				},
				remember
			),
		/blocked/
	);
	assert.equal(rememberCalls.length, 0);

	rememberCalls.length = 0;
	openUrl = null;
	await mod.playSpotifyRow({ ...row, open_url: null }, open, remember);
	assert.equal(openUrl, null);
	assert.equal(rememberCalls.length, 0);
});
