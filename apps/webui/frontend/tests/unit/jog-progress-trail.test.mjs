import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const filename = new URL('../../src/lib/components/rb/deck/JogDial.svelte', import.meta.url);
const source = readFileSync(filename, 'utf8');

test('jog trail starts at the fixed zero tick and follows presented track progress', () => {
	assert.ok(source.includes('class="progress-zero"'));
	assert.ok(source.includes('class="progress-trail"'));
	assert.match(source, /stroke-dasharray=\{`\$\{\(tickAngle \/ 360\) \* dialCircumference\}/);
	// Clamped to [0, 1] and snapped to the rim's device pixels (pixel-snap.test.mjs).
	assert.match(source, /snapFractionToPixels\(jogPositionMs \/ deck\.duration_ms, dialCircumference \* 1\.04 \* devicePixelRatioNow\(\)\) \* 360/);
	// ANIM-CLOCK-01: the trail follows the shared per-deck playhead clock, which is the presented position projected.
	assert.match(source, /const jogPositionMs: number = \$derived\(playheadMs\(deck\.deck_id, deck\)\);/);
	compile(source, { filename: filename.pathname, generate: 'client' });
});
