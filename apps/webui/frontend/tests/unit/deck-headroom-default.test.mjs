import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const stateSource = readFileSync(
	fileURLToPath(new URL('../../src/lib/player/state.svelte.ts', import.meta.url)),
	'utf8'
);
const constantsSource = readFileSync(
	fileURLToPath(new URL('../../src/lib/player/constants.ts', import.meta.url)),
	'utf8'
);
const profileSource = readFileSync(
	fileURLToPath(new URL('../../../../../apps/autodj/profile.py', import.meta.url)),
	'utf8'
);

test('the live deck default and offline renderer use the same -6 dB pre-gain', () => {
	assert.match(constantsSource, /DECK_DEFAULT_PRE_GAIN_DB\s*=\s*-6/);
	assert.match(stateSource, /trim:\s*DECK_DEFAULT_PRE_GAIN/);
	assert.match(profileSource, /DECK_LOAD_TRIM_DB\s*=\s*HEADROOM_DB/);
});
