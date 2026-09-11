import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const source = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/HotCueBank.svelte', import.meta.url)),
	'utf8'
);

test('filled hot-cue pads do not consume main-view space with timestamps', () => {
	assert.doesNotMatch(source, /class="cue-time"/);
	assert.doesNotMatch(source, /\.cue-time\s*\{/);
});

test('loop hot-cue hover detail reports exact endpoints and decoded beat and bar length', () => {
	assert.match(source, /title=\{entry\.cue === null[\s\S]*?: hotCueTitle\(entry\.cue, deck\.anlz\?\.beatgrid\.beats \?\? \[\]\)\}/);
	assert.doesNotMatch(source, /cue\.beat_loop_size/);
});
