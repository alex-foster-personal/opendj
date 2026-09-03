import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';

const ENGINE = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');

test('centered FILTER has a real dry route around both BiquadFilterNodes', () => {
	assert.match(ENGINE, /filterDry: GainNode;/);
	assert.match(ENGINE, /filterWet: GainNode;/);
	assert.match(ENGINE, /const \{ lpHz, hpHz, dryGain, wetGain \} = _filterParamsFromKnob\(ch\.filter\);/);
	assert.match(ENGINE, /high\.connect\(filterDry\);/);
	assert.match(ENGINE, /filterHp\.connect\(filterWet\);/);
	assert.match(ENGINE, /filterDry\.connect\(cue\);/);
	assert.match(ENGINE, /filterWet\.connect\(cue\);/);
	assert.match(ENGINE, /filterDry\.connect\(fader\);/);
	assert.match(ENGINE, /filterWet\.connect\(fader\);/);
	assert.match(ENGINE, /_setParam\(nodes\.filterDry\.gain, dryGain\);/);
	assert.match(ENGINE, /_setParam\(nodes\.filterWet\.gain, wetGain\);/);
});
