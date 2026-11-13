/**
 * PLAY-5884: under Beat Sync Max, AutoPlay ranks tracks with no usable beat
 * grid last, and still plays one when it is the only candidate (never silence).
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let feed;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	feed = await loadTypeScriptModule('src/lib/components/rb/browser/autoplay-browser-feed.ts');
});

/** One feed row; `has_beatgrid` omitted means unknown. */
function row(stable_id, key, bpm, has_beatgrid) {
	return {
		stable_id,
		key,
		bpm,
		file_exists: true,
		...(has_beatgrid === undefined ? {} : { has_beatgrid })
	};
}

/** A pick from `source` (8A, 120 BPM) with the usual Beat Sync bounds. */
function pick(playlist, extra = {}) {
	return mod.pickNextStableId({
		playlist,
		current_stable_id: 'source',
		current_key: '8A',
		current_bpm: 120,
		exclude_ids: new Set(),
		played_ids: new Set(),
		enforce_play_order: false,
		min_tempo_ratio: 0.84,
		max_tempo_ratio: 1.16,
		...extra
	});
}

describe('[PLAY-5884] AutoPlay prefers gridded tracks under Beat Sync Max', () => {
	const playlist = [
		row('source', '8A', 120, true),
		row('gridless', '8A', 120, false),
		row('gridded', '8A', 121, true)
	];

	it('picks a gridless row last when Beat Sync Max is on', () => {
		assert.equal(pick(playlist, { prefer_beatgrid: true }), 'gridded');
		assert.equal(pick(playlist, { prefer_beatgrid: true, maximize_reach: true }), 'gridded');
	});

	it('keeps membership order when Beat Sync Max is off', () => {
		assert.equal(pick(playlist), 'gridless');
	});

	it('still picks a gridless row when it is the only candidate', () => {
		assert.equal(
			pick(playlist, { prefer_beatgrid: true, played_ids: new Set(['gridded']) }),
			'gridless'
		);
	});

	it('prefers a gridded row in the incompatible fallback too', () => {
		const offKey = [
			row('source', '8A', 120, true),
			row('gridless', '1A', 90, false),
			row('gridded', '1A', 90, true)
		];
		assert.equal(pick(offKey, { prefer_beatgrid: true }), 'gridded');
		assert.equal(pick(offKey), 'gridless');
	});

	it('treats a row with unknown grid presence as gridded', () => {
		const unknown = [
			row('source', '8A', 120),
			row('unknown', '8A', 120),
			row('gridless', '8A', 120, false)
		];
		assert.equal(pick(unknown, { prefer_beatgrid: true }), 'unknown');
	});

	it('the browser feed carries has_beatgrid from the playlist row', () => {
		const [carried, absent] = feed.autoPlayTrackRowsFromBrowser([
			{ stable_id: 'a', key: '8A', bpm: 120, file_exists: true, has_beatgrid: false },
			{ stable_id: 'b', key: '8A', bpm: 120, file_exists: true }
		]);
		assert.equal(carried.has_beatgrid, false);
		assert.equal('has_beatgrid' in absent, false);
		mod.setAutoPlayTrackFeed('pl', [carried]);
		assert.equal(mod.getAutoPlayPlaylist()[0].has_beatgrid, false);
	});
});
