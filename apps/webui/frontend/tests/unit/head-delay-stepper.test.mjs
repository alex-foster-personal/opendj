// requirement: MIXUX-08
// [if] head-delay control is rendered [then] adjacent larger stepper buttons exist
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('head delay uses adjacent stepper buttons with larger chevrons', async () => {
	const src = await readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8');
	assert.match(src, /hp-delay-stepper/);
	assert.match(src, /stepHeadDelay\(1\)/);
	assert.match(src, /stepHeadDelay\(-1\)/);
	assert.match(src, /width="10" height="6"/);
	assert.doesNotMatch(src, /type="number"/);
});
