/**
 * A stopped deck is not drifting. Its jog warning and waveform playhead are
 * presentation-only states so pause cannot affect sync or transport behavior.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const jogDial = source('../../src/lib/components/rb/deck/JogDial.svelte');
const waveRow = source('../../src/lib/components/rb/wave/WaveRow.svelte');
const render = source('../../src/lib/components/rb/wave/render.ts');

test('jog neutrality follows audible output, not an unpresented pause request', () => {
	assert.ok(
		/deck\.is_master \|\| !deck\.audible \? null : _offTempoTitle\(liveBpm, masterBpm\)/.test(jogDial),
		'jog warning must persist until the output-presented deck becomes inaudible'
	);
});

test('stopped waveform playhead is white before sync status is evaluated', () => {
	const tone = waveRow.slice(
		waveRow.indexOf('const syncPlayheadTone'),
		waveRow.indexOf('// Vocal state tooltip')
	);
	assert.match(tone, /if \(!deck\.audible\) return 'stopped';/);
	assert.doesNotMatch(tone, /deck\.playing/);
	assert.ok(
		tone.indexOf("if (!deck.audible) return 'stopped';") <
			tone.indexOf('followerSyncPlayheadTone'),
		'stopped state must short-circuit visual drift classification'
	);
	assert.match(render, /stopped: '#fff'/);
	assert.match(waveRow, /drawPlayhead\(ctx, cssW, cssH, syncPlayheadTone\)/);
});
