import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const blend = await loadTypeScriptModule('src/lib/rb/sync-seek-blend.ts');

test('beatFourLeadInSec finds n=4 at or before the landing', () => {
	const beats = [
		{ t: 0.0, n: 1 },
		{ t: 0.5, n: 2 },
		{ t: 1.0, n: 3 },
		{ t: 1.5, n: 4 },
		{ t: 2.0, n: 1 },
		{ t: 2.5, n: 2 },
		{ t: 3.0, n: 3 },
		{ t: 3.5, n: 4 },
		{ t: 4.0, n: 1 }
	];
	assert.equal(blend.beatFourLeadInSec(beats, 2.0), 1.5);
	assert.equal(blend.beatFourLeadInSec(beats, 2.6), 1.5);
	assert.equal(blend.beatFourLeadInSec(beats, 3.5), 3.5);
	assert.equal(blend.beatFourLeadInSec(beats, 0.2), null);
});

test('syncSeekBlendDurationSec spans beat4 → landing at tempo', () => {
	const dur = blend.syncSeekBlendDurationSec(1.5, 2.0, 1);
	assert.ok(dur >= 0.22 && dur <= 0.85);
	assert.equal(blend.syncSeekBlendDurationSec(2.0, 2.0, 1), 0.35);
});
