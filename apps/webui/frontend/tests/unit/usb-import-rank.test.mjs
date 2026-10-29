// Pin a2da8ce1b12a: USB import ranks analysis around the set that is playing,
// and keeps a short likely-next list instead of treating every file the same.

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let rank;

before(async () => {
	rank = await loadTypeScriptModule('src/lib/rb/usb-import-rank.ts');
});

function track(over) {
	return {
		id: 't',
		title: 'Track',
		artist: null,
		bpm: 128,
		key: '8A',
		play_count: 0,
		has_analysis: true,
		file_present: true,
		...over
	};
}

describe('usb import rank', () => {
	it('puts a playable fit ahead of a hugely played track that does not fit', () => {
		const plan = rank.rankUsbImport(
			[
				track({ id: 'popular', title: 'Popular', play_count: 99999, bpm: 140, key: '3A' }),
				track({ id: 'fit', title: 'Fit', play_count: 0, bpm: 128, key: '8A' })
			],
			{ playing: true, bpm: 128, key: '8A' }
		);
		assert.equal(plan.next[0].id, 'fit');
		assert.match(plan.headline, /A deck is playing/);
		assert.equal(plan.performing, true);
	});

	it('treats half-time as a fit and analyzes a missing grid first', () => {
		const plan = rank.rankUsbImport(
			[track({ id: 'half', title: 'Half', bpm: 64, has_analysis: false, play_count: 1 })],
			{ playing: true, bpm: 128, key: '8A' }
		);
		assert.equal(plan.next[0].id, 'half');
		assert.match(plan.next[0].reason, /half-time/);
		assert.match(plan.next[0].reason, /Needs a grid/);
	});

	it('keeps a missing file off the likely-next bar', () => {
		const plan = rank.rankUsbImport(
			[
				track({ id: 'gone', title: 'Gone', file_present: false, play_count: 80 }),
				track({ id: 'ok', title: 'Ok', play_count: 1 })
			],
			{ playing: true, bpm: 128, key: '8A' }
		);
		assert.deepEqual(
			plan.next.map((row) => row.id),
			['ok']
		);
		assert.equal(
			plan.queue.find((row) => row.id === 'gone').phase,
			'skip'
		);
	});

	it('caps how many grids are analyzed now while a deck is playing', () => {
		const tracks = [];
		for (let i = 1; i <= 30; i += 1) {
			tracks.push(
				track({
					id: `t${i}`,
					title: `T${i}`,
					play_count: i,
					has_analysis: false
				})
			);
		}
		const plan = rank.rankUsbImport(tracks, { playing: true, bpm: 128, key: '8A' });
		assert.deepEqual(
			plan.next.map((row) => row.play_count),
			[30, 29, 28, 27, 26]
		);
		assert.equal(plan.counts.next, rank.USB_IMPORT_NEXT_CAP);
		assert.equal(plan.counts.soon, rank.USB_IMPORT_SOON_CAP - rank.USB_IMPORT_NEXT_CAP);
		assert.equal(plan.counts.later, 30 - rank.USB_IMPORT_SOON_CAP);
		const queued = plan.queue.filter((row) => row.reason.startsWith('Queued behind'));
		assert.equal(queued.length, 30 - rank.USB_IMPORT_SOON_CAP);
	});

	it('analyzes the most-played tracks first when nothing is loaded', () => {
		const plan = rank.rankUsbImport(
			[
				track({ id: 'low', title: 'Low', play_count: 2, has_analysis: false, bpm: null, key: null }),
				track({ id: 'high', title: 'High', play_count: 9, has_analysis: false, bpm: null, key: null }),
				track({ id: 'done', title: 'Done', play_count: 50, has_analysis: true, bpm: null, key: null })
			],
			{ playing: false, bpm: null, key: null }
		);
		assert.equal(plan.next[0].id, 'high');
		assert.match(plan.headline, /Nothing is loaded/);
		assert.equal(plan.performing, false);
	});

	it('says so when the stick has no tracks', () => {
		const plan = rank.rankUsbImport([], { playing: false, bpm: null, key: null });
		assert.equal(plan.next.length, 0);
		assert.match(plan.headline, /no tracks/);
	});

	it('throws on a play count the wire parser should have refused, instead of ranking it as zero', () => {
		// Sol P1, PR #6223. Control: a real count of 0 still ranks.
		const ctx = { playing: false, bpm: null, key: null };
		assert.doesNotThrow(() => rank.rankUsbImport([track({ id: 'z', play_count: 0 })], ctx));
		for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY]) {
			assert.throws(() => rank.rankUsbImport([track({ id: 'bad', play_count: bad })], ctx), /USB track bad has play count/);
		}
	});
});
