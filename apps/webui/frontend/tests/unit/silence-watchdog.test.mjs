import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * P0 (audio-never-cuts-out-under-thrash, defect D2): "a deck says it is
 * playing" and "signal is actually leaving the master bus" are two different
 * claims, and today nothing in the app compares them.
 *
 * On Wed 2 Sep 2026 audio stopped at 12:58 CEST and the decks went on
 * reporting themselves as playing for the next ~24 minutes. `peekDeckMeter()`
 * reads a real per-deck AnalyserNode RMS, but it feeds a cosmetic VFader pulse
 * and nothing else - there is no master-bus analyser at all, and no code path
 * anywhere turns a sustained zero into a verdict. The sibling ledger item
 * `deck-fully-silent-while-playing` recorded the same symptom from the other
 * direction (gain reaching zero) in round 1 and it is still unfixed.
 *
 * THIS MODULE DOES NOT EXIST YET. `src/lib/rb/silence-watchdog.ts` is the
 * contract these tests define, and the failures below are the specification.
 * It is deliberately a PURE FOLD over samples rather than a class that owns a
 * timer, for the same reason `xrun-math.ts` is pure: the thing being tested is
 * the decision, and a decision that can only be observed by waiting out a real
 * timer on a thrashing laptop is a decision nobody can test.
 *
 * DEVICE-AGNOSTIC BY CONSTRUCTION: the input is `{playing, masterRms, tMs}`.
 * Nothing about a device, a driver, or a transport - just "it claims to be
 * playing and no sound is coming out".
 *
 * Regression lines:
 * - if sustained silence under a playing deck emits no verdict then the app
 *   still cannot tell a dropout from a healthy set
 * - if silence with nothing playing emits a verdict then a stopped deck is a
 *   false alarm and the channel gets ignored
 * - if audible signal does not reset the clock then one quiet passage arms the
 *   alarm for the rest of the track
 * - if the verdict re-fires per sample then one dropout is a toast storm
 */

const WATCHDOG_MODULE = 'src/lib/rb/silence-watchdog.ts';

let silence = null;
let loadError = null;

before(async () => {
	try {
		silence = await loadTypeScriptModule(WATCHDOG_MODULE);
	} catch (error) {
		loadError = error;
	}
});

function _silence() {
	assert.equal(
		loadError,
		null,
		`if ${WATCHDOG_MODULE} does not exist then broken - nothing compares "the deck says ` +
			'playing" against "signal is leaving the master bus", so a deck can report ' +
			'playing:true into total silence indefinitely, which is what happened for ~24 ' +
			`minutes on Wed 2 Sep 2026 (bundler said: ${loadError === null ? '' : String(loadError)})`
	);
	return silence;
}

/**
 * Fold a described run of samples and return every verdict it emitted.
 *
 * `sampleMs` is the cadence a caller would poll the meter at; the window is
 * read from the module so the test cannot drift from the shipped constant.
 */
function runSamples(samples, sampleMs = 100) {
	const mod = _silence();
	const verdicts = [];
	let state;
	let tMs = 0;
	for (const sample of samples) {
		state = mod.foldSilenceSample(state, { ...sample, tMs });
		if (state.verdict !== 'ok') verdicts.push({ tMs, verdict: state.verdict });
		tMs += sampleMs;
	}
	return { verdicts, state };
}

/** A run of identical samples long enough to span `ms`. */
function heldFor(ms, sample, sampleMs = 100) {
	return Array.from({ length: Math.ceil(ms / sampleMs) + 1 }, () => sample);
}

//-----------------------------------------------------------------------------
// the verdict it exists to produce
//-----------------------------------------------------------------------------

test('sustained silence under a playing deck emits silent-while-playing', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS + 500, { playing: true, masterRms: 0 })
	);
	assert.equal(
		verdicts.length,
		1,
		'if a playing deck producing masterRms 0 for longer than the watchdog window emits ' +
			`${verdicts.length} verdict(s) instead of 1 then broken - this is the Wed 2 Sep ` +
			'2026 failure exactly, and the app currently produces nothing at all'
	);
	assert.equal(verdicts[0].verdict, 'silent-while-playing');
	assert.ok(
		verdicts[0].tMs >= mod.SILENT_WHILE_PLAYING_MS,
		'if the verdict fires before the window has elapsed then broken - a momentary gap ' +
			'between tracks would alarm on every mix'
	);
});

test('silence shorter than the window is not a verdict', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS - 300, { playing: true, masterRms: 0 })
	);
	assert.equal(
		verdicts.length,
		0,
		'if silence briefer than the window emits a verdict then broken - every cue point, ' +
			'every gap between phrases, and every load becomes an alarm'
	);
});

//-----------------------------------------------------------------------------
// the two ways it must NOT fire
//-----------------------------------------------------------------------------

test('silence with nothing playing is never a verdict', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS * 4, { playing: false, masterRms: 0 })
	);
	assert.equal(
		verdicts.length,
		0,
		'if a stopped deck in silence emits silent-while-playing then broken - that is the ' +
			'normal state of the app whenever nobody is playing anything, and an alarm that ' +
			'fires at rest is an alarm the operator mutes'
	);
});

test('signal above the silence floor is never a verdict', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS * 4, {
			playing: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 2
		})
	);
	assert.equal(
		verdicts.length,
		0,
		'if audible signal emits silent-while-playing then broken - the watchdog would ' +
			'fire through a perfectly good set'
	);
});

//-----------------------------------------------------------------------------
// the behaviours that decide whether it is usable rather than merely correct
//-----------------------------------------------------------------------------

test('real signal returning resets the silence clock', () => {
	const mod = _silence();
	const nearlyThere = heldFor(mod.SILENT_WHILE_PLAYING_MS - 300, { playing: true, masterRms: 0 });
	const { verdicts } = runSamples([
		...nearlyThere,
		{ playing: true, masterRms: mod.SILENCE_RMS_FLOOR * 10 },
		...nearlyThere
	]);
	assert.equal(
		verdicts.length,
		0,
		'if audible signal does not reset the clock then broken - two unrelated quiet ' +
			'passages would add up into a dropout that never happened'
	);
});

test('one dropout is one verdict, not one per sample', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS * 10, { playing: true, masterRms: 0 })
	);
	assert.equal(
		verdicts.length,
		1,
		`if a single silent episode emits ${verdicts.length} verdicts then broken - the ` +
			'watchdog drives a toast and an escalated perf event, so a per-sample verdict is ' +
			'a toast storm on top of an outage'
	);
});

test('a second dropout after recovery is a second verdict', () => {
	const mod = _silence();
	const dropout = heldFor(mod.SILENT_WHILE_PLAYING_MS + 500, { playing: true, masterRms: 0 });
	const recovered = heldFor(500, { playing: true, masterRms: mod.SILENCE_RMS_FLOOR * 10 });
	const { verdicts } = runSamples([...dropout, ...recovered, ...dropout]);
	assert.equal(
		verdicts.length,
		2,
		'if the second dropout is swallowed then broken - the watchdog would go quiet ' +
			'permanently after the first incident of a session'
	);
});

//-----------------------------------------------------------------------------
// house rules: pure, and fail-fast on nonsense
//-----------------------------------------------------------------------------

test('the fold is pure: the state handed in is never mutated', () => {
	const mod = _silence();
	const first = mod.foldSilenceSample(undefined, { playing: true, masterRms: 0, tMs: 0 });
	const snapshot = JSON.stringify(first);
	const second = mod.foldSilenceSample(first, { playing: true, masterRms: 0, tMs: 100 });
	assert.equal(
		JSON.stringify(first),
		snapshot,
		'if the fold mutates its input then broken - a caller keeping the previous state ' +
			'for comparison would silently be comparing a value against itself'
	);
	assert.notEqual(first, second);
});

test('a meter reading that is not a number is refused, not treated as silence', () => {
	const mod = _silence();
	for (const masterRms of [Number.NaN, -1, undefined]) {
		assert.throws(
			() => mod.foldSilenceSample(undefined, { playing: true, masterRms, tMs: 0 }),
			RangeError,
			`if masterRms ${String(masterRms)} is accepted then broken - a broken meter would ` +
				'read as silence and raise a dropout alarm during perfectly good audio, which ' +
				'is worse than no watchdog at all'
		);
	}
});

test('time going backwards is refused rather than folded', () => {
	const mod = _silence();
	const first = mod.foldSilenceSample(undefined, { playing: true, masterRms: 0, tMs: 1000 });
	assert.throws(
		() => mod.foldSilenceSample(first, { playing: true, masterRms: 0, tMs: 900 }),
		RangeError,
		'if a sample timestamped earlier than the last one is folded then broken - the ' +
			'elapsed-silence arithmetic would go negative and the window would never elapse'
	);
});

test('audible-only claimed live emits silent-while-playing after the window', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS + 500, { playing: false, audible: true, masterRms: 0 })
	);
	assert.equal(verdicts.length, 1);
	assert.equal(verdicts[0].verdict, 'silent-while-playing');
	assert.ok(verdicts[0].tMs >= mod.SILENT_WHILE_PLAYING_MS);
});

test('audible with signal above the floor is never a verdict', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS * 4, {
			playing: false,
			audible: true,
			masterRms: mod.SILENCE_RMS_FLOOR * 2
		})
	);
	assert.equal(verdicts.length, 0);
});

test('a second audible-only dropout after reset is a second verdict', () => {
	const mod = _silence();
	const dropout = heldFor(mod.SILENT_WHILE_PLAYING_MS + 500, {
		playing: false,
		audible: true,
		masterRms: 0
	});
	const recovered = heldFor(500, {
		playing: false,
		audible: false,
		masterRms: mod.SILENCE_RMS_FLOOR * 10
	});
	const { verdicts } = runSamples([...dropout, ...recovered, ...dropout]);
	assert.equal(verdicts.length, 2);
});

test('held master silence with source_explains_silence emits no verdict', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS + 500, {
			playing: true,
			masterRms: 0,
			source_explains_silence: true
		})
	);
	assert.equal(verdicts.length, 0);
});

test('held master silence without source flag still emits one verdict', () => {
	const mod = _silence();
	const { verdicts } = runSamples(
		heldFor(mod.SILENT_WHILE_PLAYING_MS + 500, { playing: true, masterRms: 0 })
	);
	assert.equal(verdicts.length, 1);
	assert.equal(verdicts[0].verdict, 'silent-while-playing');
});

test('source_explains_silence resets the silence clock before the window', () => {
	const mod = _silence();
	const nearlyThere = heldFor(mod.SILENT_WHILE_PLAYING_MS - 300, {
		playing: true,
		masterRms: 0
	});
	const { verdicts } = runSamples([
		...nearlyThere,
		{ playing: true, masterRms: 0, source_explains_silence: true },
		...nearlyThere
	]);
	assert.equal(verdicts.length, 0);
});

test('positive control: source flag clears then dropout fires after window', () => {
	const mod = _silence();
	const explained = heldFor(500, {
		playing: true,
		masterRms: 0,
		source_explains_silence: true
	});
	const dropout = heldFor(mod.SILENT_WHILE_PLAYING_MS + 500, {
		playing: true,
		masterRms: 0,
		source_explains_silence: false
	});
	const { verdicts } = runSamples([...explained, ...dropout]);
	assert.equal(verdicts.length, 1);
	assert.equal(verdicts[0].verdict, 'silent-while-playing');
});
