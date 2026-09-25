// requirement: MIXUX-08
// [if] crossfader thumb visual shrinks [then] hit width stays at baseline
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('hit width is unchanged while visual width is thinner', async () => {
	const geom = await loadTypeScriptModule('src/lib/rb/crossfader-geometry.ts');
	assert.equal(geom.CROSSFADER_THUMB_HIT_W, 12);
	assert.ok(geom.CROSSFADER_THUMB_VIS_W < geom.CROSSFADER_THUMB_HIT_W);
	assert.ok(geom.CROSSFADER_THUMB_VIS_H > 12);
});

test('pointer math still uses hit width', async () => {
	const geom = await loadTypeScriptModule('src/lib/rb/crossfader-geometry.ts');
	const mid = geom.crossfaderValueFromPointerX(100, 0, 200);
	const left = geom.crossfaderThumbLeftPx(mid, 200);
	assert.ok(left >= 0);
	assert.ok(left <= 200 - geom.CROSSFADER_THUMB_HIT_W);
});
