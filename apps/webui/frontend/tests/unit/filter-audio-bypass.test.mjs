import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';

// deck-channel-graph.ts was extracted from audio-engine.svelte.ts on
// Sat 12 Sep 2026 (commit 1db624ef7, #2155 output-stall recovery) and now
// owns the per-deck node construction and wiring this file pins; the mixer
// knob update path (audio-engine.svelte.ts's _setParam calls) stayed behind.
// Read both so the extraction does not leave this guard un-anchored.
const ENGINE =
	readFrontendSource('src/lib/rb/audio-engine.svelte.ts') +
	'\n' +
	readFrontendSource('src/lib/rb/deck-channel-graph.ts');

test('centered FILTER has a real dry route around both BiquadFilterNodes', () => {
	assert.match(ENGINE, /filterDry: GainNode;/);
	assert.match(ENGINE, /filterLpWet: GainNode;/);
	assert.match(ENGINE, /filterHpWet: GainNode;/);
	assert.match(ENGINE, /const \{ lpHz, hpHz, dryGain, lpWetGain, hpWetGain \} = filterParamsFromKnob\(ch\.filter\);/);
	assert.match(ENGINE, /for \(const stage of \[filterDry, filterLp, filterHp\]\) high\.connect\(stage\);/);
	assert.match(
		ENGINE,
		/for \(const branch of \[filterDry, filterLpWet, filterHpWet\]\) branch\.connect\(cue\);/
	);
	assert.match(
		ENGINE,
		/for \(const branch of \[filterDry, filterLpWet, filterHpWet\]\) branch\.connect\(fader\);/
	);
	assert.match(ENGINE, /_setParam\(nodes\.filterDry\.gain, dryGain\);/);
	assert.match(ENGINE, /_setParam\(nodes\.filterLpWet\.gain, lpWetGain\);/);
	assert.match(ENGINE, /_setParam\(nodes\.filterHpWet\.gain, hpWetGain\);/);
});

test('the inactive filter half is silenced, not left in series', () => {
	// Issue #990 follow-up (PR #1021, discussion_r3967244006): filterLp used to
	// feed filterHp directly, so the "inactive" side stayed in the signal path
	// at its gentlest cutoff instead of being genuinely bypassed. Each biquad
	// must instead branch straight off `high` into its OWN wet gain, with no
	// lp-to-hp (or hp-to-lp) chain anywhere in the engine.
	assert.doesNotMatch(ENGINE, /filterLp\.connect\(filterHp\)/);
	assert.doesNotMatch(ENGINE, /filterHp\.connect\(filterLp\)/);
	assert.match(ENGINE, /filterLp\.connect\(filterLpWet\);/);
	assert.match(ENGINE, /filterHp\.connect\(filterHpWet\);/);
});
