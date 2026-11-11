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
const COLOR =
	readFrontendSource('src/lib/rb/color-fx.ts') +
	'\n' +
	readFrontendSource('src/lib/rb/color-fx-graph.ts');

test('centered FILTER has a real dry route around both BiquadFilterNodes', () => {
	assert.match(ENGINE, /filterDry: GainNode;/);
	assert.match(ENGINE, /filterLpWet: GainNode;/);
	assert.match(ENGINE, /filterHpWet: GainNode;/);
	// COLORFX-01: the branches now sum on a unity color bus that feeds the cue
	// and the fader, and the knob drive (with its per-effect law) lives in
	// color-fx.ts / color-fx-graph.ts. FILTER mode is filterParamsFromKnob verbatim
	// (pinned behaviorally in color-fx.test.mjs).
	assert.match(
		ENGINE,
		/const \{ lpHz, hpHz, dryGain, lpWetGain, hpWetGain \} = colorFilterParamsForBuild\(\s*deck,\s*ch\.filter\s*\);/
	);
	assert.match(
		ENGINE,
		/for \(const stage of \[filterDry, filterLp, filterHp\]\) high\.connect\(stage\);/
	);
	assert.match(
		ENGINE,
		/for \(const branch of \[filterDry, filterLpWet, filterHpWet\]\) branch\.connect\(colorBus\);/
	);
	assert.match(ENGINE, /colorBus\.connect\(cue\);/);
	assert.match(ENGINE, /colorBus\.connect\(fader\);/);
	assert.match(COLOR, /filter: filterParamsFromKnob\(value\)/);
	assert.match(COLOR, /_ramp\(nodes\.filterDry\.gain, f\.dryGain, now, COLOR_FX_RAMP_SEC\);/);
	assert.match(COLOR, /_ramp\(nodes\.filterLpWet\.gain, f\.lpWetGain, now, COLOR_FX_RAMP_SEC\);/);
	assert.match(COLOR, /_ramp\(nodes\.filterHpWet\.gain, f\.hpWetGain, now, COLOR_FX_RAMP_SEC\);/);
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
