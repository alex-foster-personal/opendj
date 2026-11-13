// requirement: EDGESAMP-13977
/**
 * Issue #10778 (S1): the edge sampler tab's repeat toggle, once -> loop -> 4 -> 2 -> 8 -> once
 * (MOCK-PARITY row "Repeat toggle"), and what each repeat plays.
 *
 * Regression lines:
 * - if nextRepeat is applied five times from once then it is once again
 * - if repeat is 8 on a 4-beat region then the voice loops 4 beats, never past the region
 * - if a slot comes from a saved loop then it starts looping, a plain cue plays once
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let repeat;
let types;
before(async () => {
	repeat = await loadTypeScriptModule('src/lib/player/sampler/sampler-repeat.ts');
	types = await loadTypeScriptModule('src/lib/player/sampler/sampler-types.ts');
});

test('the toggle cycles once, loop, 4, 2, 8 and five clicks come back', () => {
	/** [if] nextRepeat runs five times from once [then] it visits loop, 4, 2, 8 and returns to once, [else stop]. */
	const seen = ['once'];
	for (let i = 0; i < 5; i++) seen.push(repeat.nextRepeat(seen.at(-1)));
	assert.deepEqual(seen, ['once', 'loop', 4, 2, 8, 'once']);
	assert.deepEqual([...repeat.REPEAT_CYCLE], ['once', 'loop', 4, 2, 8]);
});

test('a numeric repeat loops that many beats, capped at the region', () => {
	/** [if] repeat is 8 on a 4-beat region or 2 on a 16-beat one [then] it loops 4 and 2 beats, [else stop]. */
	assert.equal(repeat.loopBeats(8, 4), 4);
	assert.equal(repeat.loopBeats(2, 16), 2);
	assert.equal(repeat.loopBeats(4, 16), 4);
	assert.equal(repeat.loopBeats('loop', 16), 16);
	assert.equal(repeat.loopBeats('once', 16), null);
});

test('a saved loop defaults to loop and a plain cue to once', () => {
	/** [if] defaultRepeat gets a loop or a plain cue [then] it answers loop or once, and DEFAULT_SETTINGS loops, [else stop]. */
	assert.equal(repeat.defaultRepeat(true), 'loop');
	assert.equal(repeat.defaultRepeat(false), 'once');
	assert.equal(types.DEFAULT_SETTINGS.repeat, 'loop');
});

test('each repeat names what it plays in its tooltip', () => {
	/** [if] repeatTip is asked for once, loop over 1 and 4 bars, and 4 [then] each sentence says what plays, [else stop]. */
	assert.match(repeat.repeatTip('once', 1), /^Play once: /);
	assert.equal(repeat.repeatTip('loop', 1), 'Loop the whole saved loop (1 bar)');
	assert.equal(repeat.repeatTip('loop', 4), 'Loop the whole saved loop (4 bars)');
	assert.equal(repeat.repeatTip(4, 4), 'Loop the first 4 beats');
});

test('a tab shows its rename else its title, and counts whole bars', () => {
	/** [if] a slot has no rename or a 2-beat region [then] the title shows and it counts as 1 bar, [else stop]. */
	assert.equal(types.displayName({ name: null, title: 'Track' }), 'Track');
	assert.equal(types.displayName({ name: 'Mine', title: 'Track' }), 'Mine');
	assert.equal(types.regionBars({ beats: 2 }), 1);
	assert.equal(types.regionBars({ beats: 16 }), 4);
});
