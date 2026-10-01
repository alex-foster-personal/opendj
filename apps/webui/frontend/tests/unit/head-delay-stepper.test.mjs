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

test('the head delay field steps on the arrow keys', async () => {
	const src = await readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8');
	// The field is type=text, so the browser gives it no arrow stepping of its own.
	assert.match(src, /aria-label="head delay milliseconds"[^>]*onkeydown=\{keyDelay\}/);
	const handler = src.slice(src.indexOf('function keyDelay('), src.indexOf('function scrollDelay('));
	assert.match(handler, /event\.preventDefault\(\);\s*stepHeadDelay\(event\.key === 'ArrowUp' \? 1 : -1\);/);
	// Control: every other key is left alone, so typing digits still works.
	assert.match(handler, /if \(event\.key !== 'ArrowUp' && event\.key !== 'ArrowDown'\) return;/);
});
