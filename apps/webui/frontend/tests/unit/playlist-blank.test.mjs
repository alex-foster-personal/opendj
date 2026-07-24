import assert from 'node:assert/strict';
import { before, beforeEach, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let DEFAULT_PLAYLIST_NAME;
let BLANK_PLAYLIST_GRACE_MS;
let isBlankPlaylist;
let isBlankPlaylistName;
let decideBlankPlaylistDelete;
let collectBlankPlaylistDeletes;
let markPlaylistCreateGrace;
let _resetCreateGraceForTests;

before(async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/playlist-blank.ts');
	DEFAULT_PLAYLIST_NAME = mod.DEFAULT_PLAYLIST_NAME;
	BLANK_PLAYLIST_GRACE_MS = mod.BLANK_PLAYLIST_GRACE_MS;
	isBlankPlaylist = mod.isBlankPlaylist;
	isBlankPlaylistName = mod.isBlankPlaylistName;
	decideBlankPlaylistDelete = mod.decideBlankPlaylistDelete;
	collectBlankPlaylistDeletes = mod.collectBlankPlaylistDeletes;
	markPlaylistCreateGrace = mod.markPlaylistCreateGrace;
	_resetCreateGraceForTests = mod._resetCreateGraceForTests;
});

beforeEach(() => {
	_resetCreateGraceForTests();
});

describe('isBlankPlaylistName / isBlankPlaylist', () => {
	it('treats create-default and untitled sentinels as blank names', () => {
		assert.equal(isBlankPlaylistName(DEFAULT_PLAYLIST_NAME), true);
		assert.equal(isBlankPlaylistName('  new playlist  '), true);
		assert.equal(isBlankPlaylistName('Untitled'), true);
		assert.equal(isBlankPlaylistName('untitled playlist'), true);
		assert.equal(isBlankPlaylistName(''), true);
	});

	it('treats user-chosen names as non-blank identity', () => {
		assert.equal(isBlankPlaylistName('Pleasure'), false);
		assert.equal(isBlankPlaylistName('New Playlist 2'), false);
	});

	it('requires empty membership AND blank name', () => {
		assert.equal(
			isBlankPlaylist({
				playlist_id: 'a',
				name: DEFAULT_PLAYLIST_NAME,
				track_count: 0
			}),
			true
		);
		assert.equal(
			isBlankPlaylist({
				playlist_id: 'a',
				name: DEFAULT_PLAYLIST_NAME,
				track_count: 3
			}),
			false
		);
		assert.equal(
			isBlankPlaylist({ playlist_id: 'a', name: 'Warmup', track_count: 0 }),
			false
		);
	});
});

describe('decideBlankPlaylistDelete safety', () => {
	it('NEVER deletes a non-blank empty playlist (renamed, zero tracks)', () => {
		const d = decideBlankPlaylistDelete({
			playlist_id: 'keep-renamed',
			name: 'My Empty Set',
			track_count: 0,
			vendor: 'webui'
		});
		assert.equal(d.action, 'keep');
		assert.match(d.reason, /renamed/i);
	});

	it('NEVER deletes a playlist that has tracks even if name is default', () => {
		const d = decideBlankPlaylistDelete({
			playlist_id: 'keep-tracks',
			name: DEFAULT_PLAYLIST_NAME,
			track_count: 2,
			vendor: 'webui'
		});
		assert.equal(d.action, 'keep');
		assert.match(d.reason, /track/i);
	});

	it('NEVER deletes non-webui vendor rows', () => {
		const d = decideBlankPlaylistDelete({
			playlist_id: 'rb-1',
			name: DEFAULT_PLAYLIST_NAME,
			track_count: 0,
			vendor: 'rekordbox'
		});
		assert.equal(d.action, 'keep');
		assert.match(d.reason, /vendor/i);
	});

	it('marks blank untitled empties as delete candidates outside grace', () => {
		const d = decideBlankPlaylistDelete(
			{
				playlist_id: 'stale-blank',
				name: 'New Playlist',
				track_count: 0,
				vendor: 'webui'
			},
			1_000_000
		);
		assert.equal(d.action, 'delete');
		assert.match(d.reason, /blank/i);
	});

	it('keeps newly created blank within grace period', () => {
		const now = 5_000_000;
		markPlaylistCreateGrace('fresh', now);
		const d = decideBlankPlaylistDelete(
			{
				playlist_id: 'fresh',
				name: DEFAULT_PLAYLIST_NAME,
				track_count: 0,
				vendor: 'webui'
			},
			now + BLANK_PLAYLIST_GRACE_MS - 1
		);
		assert.equal(d.action, 'keep');
		assert.match(d.reason, /grace/i);
	});

	it('deletes the same blank once grace elapses', () => {
		const now = 5_000_000;
		markPlaylistCreateGrace('fresh', now);
		const d = decideBlankPlaylistDelete(
			{
				playlist_id: 'fresh',
				name: DEFAULT_PLAYLIST_NAME,
				track_count: 0,
				vendor: 'webui'
			},
			now + BLANK_PLAYLIST_GRACE_MS
		);
		assert.equal(d.action, 'delete');
	});
});

describe('collectBlankPlaylistDeletes logging', () => {
	it('logs delete decisions and skip reasons; never returns renamed empties', () => {
		const logs = [];
		const ids = collectBlankPlaylistDeletes(
			[
				{
					playlist_id: 'named-empty',
					name: 'Keep Me',
					track_count: 0,
					vendor: 'webui'
				},
				{
					playlist_id: 'full',
					name: DEFAULT_PLAYLIST_NAME,
					track_count: 4,
					vendor: 'webui'
				},
				{
					playlist_id: 'stale',
					name: 'untitled',
					track_count: 0,
					vendor: 'webui'
				}
			],
			10_000,
			(msg) => logs.push(msg)
		);

		assert.deepEqual(ids, ['stale']);
		assert.equal(logs.length, 2, 'empty rows only (skip + delete)');
		assert.ok(logs.some((l) => l.includes('keep named-empty') && /renamed/i.test(l)));
		assert.ok(logs.some((l) => l.includes('delete stale') && /blank/i.test(l)));
		assert.ok(!logs.some((l) => l.includes('full')), 'non-empty rows are not logged');
	});

	it('logs grace keep for a freshly created blank', () => {
		const now = 20_000;
		markPlaylistCreateGrace('grace-id', now);
		const logs = [];
		const ids = collectBlankPlaylistDeletes(
			[
				{
					playlist_id: 'grace-id',
					name: DEFAULT_PLAYLIST_NAME,
					track_count: 0,
					vendor: 'webui'
				}
			],
			now + 1,
			(msg) => logs.push(msg)
		);
		assert.deepEqual(ids, []);
		assert.equal(logs.length, 1);
		assert.match(logs[0], /keep grace-id/);
		assert.match(logs[0], /grace/i);
	});
});
