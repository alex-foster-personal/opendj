/** ENRICH-01: the enrich-on-open card says each lane state differently. */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const card = await loadTypeScriptModule('src/lib/enrich/enrich-card.ts');

const full = { total: 1274, done: 1274, missing: 0, failed: 0, declined: 0, unavailable: null };

function summary(lanes = {}, extra = {}) {
	const all = {};
	for (const lane of ['tags', 'strip', 'beatgrid', 'key', 'loudness', 'waveform']) {
		all[lane] = { ...full, ...(lanes[lane] ?? {}) };
	}
	return {
		show: true,
		analysis: { lanes: all },
		analysis_error: null,
		coverage: {
			on_disk: 1274,
			done: { stems: 0, lyrics: 1274 },
			terminal: { stems: 0, lyrics: 0 },
			failed: { stems: 0, lyrics: 0 },
			pending: { stems: 0, lyrics: 0 }
		},
		coverage_error: null,
		stems: { state: 'done', pending: 0, reason: null },
		decisions: {},
		...extra
	};
}

test('a finished library has no lines and offers no retry', () => {
	const s = summary();
	assert.deepEqual(card.analysisLines(s), []);
	assert.equal(card.lyricsLine(s), null);
	assert.equal(card.offersRetry(s), false);
});

test('queued, failed and unavailable read differently', () => {
	const s = summary({
		beatgrid: { done: 351, missing: 923 },
		key: { done: 40, failed: 4, missing: 1230, failed_reasons: { 'LaneContractError: beat past end': 4 } },
		loudness: { done: 196, missing: 1078, unavailable: 'ffmpeg lacks astats' }
	});
	const lines = card.analysisLines(s);
	const byLane = Object.fromEntries(lines.map((l) => [l.lane, l]));
	assert.equal(byLane.beatgrid.tone, 'working');
	assert.match(byLane.beatgrid.text, /351 of 1,274 done/);
	assert.equal(byLane.key.tone, 'failed');
	assert.equal(byLane.key.title, '4: LaneContractError: beat past end');
	assert.equal(byLane.loudness.tone, 'unavailable');
	assert.match(byLane.loudness.text, /cannot produce it \(ffmpeg lacks astats\)/);
	assert.equal(card.offersRetry(s), true);
});

test('declined keys are counted apart and are not a failure', () => {
	const s = summary({ key: { done: 30, declined: 10, missing: 1234, declined_reasons: { no_tonal_center: 10 } } });
	const lines = card.analysisLines(s).filter((l) => l.lane === 'key');
	assert.deepEqual(lines.map((l) => l.tone), ['working', 'note']);
	assert.equal(card.offersRetry(s), false);
});

test('lyrics progress names found, none available and still to look up', () => {
	const s = summary({}, {
		coverage: {
			on_disk: 1274,
			done: { stems: 0, lyrics: 113 },
			terminal: { stems: 0, lyrics: 206 },
			failed: { stems: 0, lyrics: 0 },
			pending: { stems: 0, lyrics: 955 }
		}
	});
	assert.equal(card.lyricsLine(s).text, 'Lyrics: 113 found, 206 with none available, 955 still to look up');
});

test('stems: asked, impossible, declined and unknown each have their own wording', () => {
	assert.equal(card.stemsText({ state: 'ask', pending: 1150, reason: null }), '1,150 tracks have no stems yet. Separate them?');
	assert.match(card.stemsText({ state: 'no_source', pending: 3, reason: 'no farm' }), /cannot make them \(no farm\)/);
	assert.equal(card.stemsText({ state: 'user_declined', pending: 3, reason: null }), null);
	assert.match(card.stemsText({ state: 'unknown', pending: null, reason: 'read failed' }), /unknown: read failed/);
});

test('an unarmed drain is said, never shown as complete', () => {
	const s = summary({}, { analysis: null, analysis_error: 'drain not running' });
	assert.deepEqual(card.analysisLines(s).map((l) => l.tone), ['unavailable']);
});
