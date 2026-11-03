// requirement: LIBPG-7694b
import assert from 'node:assert/strict';
import test from 'node:test';

import {
	daysAgoLabel,
	displayFacts,
	lastPlayedLabel,
	playFactsFromUnknown,
	playHits
} from '../../src/lib/components/rb/browser/last-played.ts';

const NOW = Date.parse('2026-10-10T12:00:00.000Z');

test('days ago uses whole elapsed days', () => {
	assert.equal(daysAgoLabel(null, NOW), '');
	assert.equal(daysAgoLabel('not-a-date', NOW), '');
	assert.equal(daysAgoLabel('2026-10-10T06:00:00.000Z', NOW), 'today');
	assert.equal(daysAgoLabel('2026-10-11T00:00:00.000Z', NOW), 'today');
	assert.equal(daysAgoLabel('2026-10-09T12:00:00.000Z', NOW), '1 day ago');
	assert.equal(daysAgoLabel('2026-10-07T12:00:00.000Z', NOW), '3 days ago');
});

test('plays payload becomes gig and rekordbox hits, and the cell uses the newest date', () => {
	const facts = playFactsFromUnknown({
		opendj_last_played_at: '2026-10-07T12:00:00.000Z',
		opendj_play_count: 2,
		rekordbox_play_count: 4,
		play_count: 6,
		stable_id: 'abc'
	});
	assert.ok(facts);
	assert.equal(lastPlayedLabel(facts, NOW), '3 days ago');
	assert.deepEqual(
		playHits(facts, NOW).map((hit) => [hit.id, hit.detail]),
		[
			['rekordbox', '4 plays'],
			['gig', '3 days ago']
		]
	);
});

test('a newer serato timestamp wins the cell, and an odj source alias is a gig', () => {
	const facts = playFactsFromUnknown({
		opendj_last_played_at: '2026-10-07T12:00:00.000Z',
		opendj_play_count: 1,
		serato_play_count: 1,
		serato_last_played_at: '2026-10-09T12:00:00.000Z',
		sources: [{ source: 'odj', count: 1, played_at: '2026-10-08T12:00:00.000Z' }]
	});
	assert.equal(lastPlayedLabel(facts, NOW), '1 day ago');
	const ids = playHits(facts, NOW).map((hit) => hit.id);
	assert.deepEqual(ids, ['gig', 'serato']);
	assert.equal(playHits(facts, NOW).find((hit) => hit.id === 'gig')?.detail, '2 days ago');
});

test('combined play_count is not a source, and a stick row count is Rekordbox USB', () => {
	assert.equal(playFactsFromUnknown({ play_count: 9 }), null);
	assert.equal(playFactsFromUnknown(null), null);
	assert.equal(playFactsFromUnknown({ rekordbox_play_count: -3, opendj_last_played_at: 'nope' }), null);
	const shown = displayFacts({
		stableId: 'usb-vol-1',
		playCount: 2,
		wire: null,
		fetched: null
	});
	assert.equal(lastPlayedLabel(shown, NOW), '');
	assert.deepEqual(
		playHits(shown, NOW).map((hit) => [hit.label, hit.detail]),
		[['Rekordbox USB', '2 plays']]
	);
	const library = displayFacts({
		stableId: 'abc',
		playCount: 5,
		wire: null,
		fetched: null
	});
	assert.deepEqual(playHits(library, NOW), []);
});

test('fetched plays replace the listing, except a stick count still counts as USB', () => {
	const wire = playFactsFromUnknown({ opendj_play_count: 1, opendj_last_played_at: '2026-10-01T12:00:00.000Z' });
	const fetched = playFactsFromUnknown({
		rekordbox_play_count: 3,
		opendj_play_count: 1,
		opendj_last_played_at: '2026-10-07T12:00:00.000Z'
	});
	const shown = displayFacts({ stableId: 'abc', playCount: 4, wire, fetched });
	assert.equal(lastPlayedLabel(shown, NOW), '3 days ago');
	assert.deepEqual(
		playHits(shown, NOW).map((hit) => hit.id),
		['rekordbox', 'gig']
	);
	const stick = displayFacts({
		stableId: 'usb-vol-9',
		playCount: 8,
		wire: null,
		fetched
	});
	assert.ok(playHits(stick, NOW).some((hit) => hit.id === 'rekordbox_usb' && hit.detail === '8 plays'));
});

test('LIBPG-7694b the table shows a Last column wired to the cell, and its hover card explains the day count', async () => {
	const { readFileSync } = await import('node:fs');
	const read = (path) => readFileSync(new URL(`../../src/lib/components/rb/browser/${path}`, import.meta.url), 'utf8');
	const table = read('TrackTable.svelte');
	assert.match(table, /<LastPlayedCell\s+stableId=\{row\.stable_id\}/);
	assert.match(table, /onColResizeStart\(e, 'last'\)/);
	const cell = read('LastPlayedCell.svelte');
	assert.match(cell, /data-testid="last-played"/);
	assert.match(cell, /data-testid="last-played"\s+data-custom-tip=""/, 'the cell is a hover-card host');
	assert.match(cell, /data-testid="last-played-explain"\s*>\{view\.label\}: days since the newest play/, 'the card explains the day count');
});

test('a failed plays fetch is not cached, so the next load retries (LIBPG-7694b)', async () => {
	const { loadTypeScriptModule } = await import('./load-typescript.mjs');
	const realFetch = globalThis.fetch;
	let calls = 0;
	let fail = true;
	globalThis.fetch = async () => {
		calls += 1;
		if (fail) throw new TypeError('network down');
		return new Response(JSON.stringify({ stable_id: 'abc', play_count: 1, rekordbox_play_count: 1 }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	try {
		const mod = await loadTypeScriptModule('src/lib/components/rb/browser/last-played-load.ts', {
			viteApiBase: 'http://127.0.0.1:9'
		});
		assert.equal(await mod.loadTrackPlays('abc'), null);
		assert.equal(mod.peekTrackPlays('abc'), undefined, 'a failure leaves the id unloaded');
		fail = false;
		const facts = await mod.loadTrackPlays('abc');
		assert.ok(facts, 'the retry loads the plays');
		assert.equal(calls, 2);
		// Control: a success is cached, so a third load fetches nothing.
		await mod.loadTrackPlays('abc');
		assert.equal(calls, 2);
	} finally {
		globalThis.fetch = realFetch;
	}
});
