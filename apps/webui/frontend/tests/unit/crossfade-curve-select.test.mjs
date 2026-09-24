// requirement: MIXUX-08
// [if] crossfade curve pref defaults [then] magic is selected and tbd curves cannot be set
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('default crossfade curve is magic', async () => {
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	assert.equal(prefs.uiPrefs.crossfade_curve, 'magic');
});

test('tbd crossfade curves are disabled in the dropdown markup', async () => {
	const src = await readFile('src/lib/components/rb/mixer/CrossfadeCurveSelect.svelte', 'utf8');
	assert.match(src, /bass swap \(tbd\)/);
	assert.match(src, /disabled=\{option\.disabled\}/);
});

test('setCrossfadeCurve rejects unbuilt curves', async () => {
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	assert.throws(() => prefs.setCrossfadeCurve('linear'), /not implemented/i);
});
