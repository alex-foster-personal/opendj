/**
 * pin 449ee2dc213e: replace vibe up/down arrows with thumbs and distinct mark
 * semantics (PREF-02). Behaviour is on main; this file is the #4087 acceptance
 * contract so the pin id is grep-able in CI.
 *
 * [if] VibeMeter still wires chevron arrows or voteVibe [then STOP]
 * [if] thumbs do not dispatch feedback_mark with bad/good/great [then STOP]
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const VIBE_METER = fileURLToPath(
	new URL('../../src/lib/components/rb/VibeMeter.svelte', import.meta.url)
);

test('pin 449ee2dc213e: VibeMeter uses thumbs that dispatch feedback_mark, not voteVibe arrows', async () => {
	const source = await readFile(VIBE_METER, 'utf8');
	assert.match(source, /aria-label="thumbs down"/);
	assert.match(source, /aria-label="thumbs up"/);
	assert.match(source, /recordFeedback\('bad', event\)/);
	assert.match(source, /event\.shiftKey \? 'great' : 'good'/);
	assert.match(source, /runPerformanceCommandFromUi\(\{ type: 'feedback_mark', vote \}/);
	assert.match(source, /<path d="M5 2\.2h5\.1/);
	assert.match(source, /<path d="M5 13\.8h5\.1/);
	assert.doesNotMatch(source, /voteVibe\(/);
	assert.doesNotMatch(source, /▲|▼|chevron/i);
});
