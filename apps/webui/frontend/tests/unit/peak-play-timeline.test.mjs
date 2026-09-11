import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let timeline;

before(async () => {
	timeline = await loadTypeScriptModule('src/lib/rb/peak-play-timeline.ts');
});

test('pushPlayed appends in oldest-first order', () => {
	assert.deepEqual(timeline.pushPlayed([], 'a'), ['a']);
	assert.deepEqual(timeline.pushPlayed(['a'], 'b'), ['a', 'b']);
});

test('pushPlayed is a no-op when playedId is already last', () => {
	const played = ['a', 'b'];
	assert.deepEqual(timeline.pushPlayed(played, 'b'), played);
});

test('pushPlayed drops oldest entries when over cap', () => {
	let played = [];
	for (let i = 0; i < timeline.PEAK_TIMELINE_CAP + 3; i += 1) {
		played = timeline.pushPlayed(played, `id-${i}`);
	}
	assert.equal(played.length, timeline.PEAK_TIMELINE_CAP);
	assert.equal(played[0], 'id-3');
	assert.equal(played[played.length - 1], `id-${timeline.PEAK_TIMELINE_CAP + 2}`);
});
