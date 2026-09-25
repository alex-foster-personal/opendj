import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Pin 5a5c3b8033d8 (PARTIAL half): the ten-segment / red-amber-green meter
// shipped in PR #1062 for the per-CHANNEL taps only. Those tap post-trim,
// post-EQ, and post-channel-fader (issue #3529, see meter-tap.ts's header)
// and so they track each deck's channel fader, not the master volume control.
// This file pins the still-open half: a MASTER OUTPUT meter, fed from the
// master bus post master-gain, reusing meter-tap.ts for measurement and
// meter-math.ts for every threshold/colour decision - never a second set of
// numbers.
//
// Regression lines:
// - if the master tap reads from anywhere except _masterGain (or a node
//   downstream of it) then the master volume control stops moving the meter,
//   which is the entire point of a MASTER meter as distinct from the
//   existing per-channel ones
// - if the master meter component invents its own thresholds/colours instead
//   of importing them from meter-math, the two meters can silently disagree
//   about what "red" means
// - if the component claims a speaker-damage guarantee, that overclaims what
//   a digital dBFS reading downstream of the app can possibly know (no amp
//   gain, no speaker power handling are visible from here)
// - if the meter runs its RAF loop unconditionally for the whole /performance
//   session (TopBar is mounted for that whole lifetime), it burns a 60fps
//   loop before anything is even loaded, let alone playing
// - if the meter's "everything is summed" honesty claim ignores external
//   USB routing, it is false for a routed deck: that deck bypasses
//   _masterGain and the crossfader entirely

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function readSrc(relative) {
	return readFileSync(`${SRC}/${relative}`, 'utf8');
}

/**
 * Pull one template-literal attribute's VALUE out of a Svelte tag, e.g.
 * `title={`...`}`. Anchoring here (rather than grepping the whole file) is
 * the point: a docstring elsewhere in the same file must never be able to
 * stand in for what the shipped UI string actually says.
 */
function extractTemplateAttr(source, attrName) {
	const match = source.match(new RegExp(`${attrName}=\\{\`([\\s\\S]*?)\`\\}`));
	assert.ok(match, `expected a ${attrName}={\`...\`} template-literal attribute in the source`);
	return match[1];
}

let audio;
let meterTap;
let masterMeterGate;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts');
	meterTap = await loadTypeScriptModule('src/lib/rb/meter-tap.ts');
	masterMeterGate = await loadTypeScriptModule('tests/unit/fixtures/master-meter-gate-entry.ts');
});

// ---------------------------------------------------------------------------
// live behaviour: the one branch this suite CAN exercise without a real
// AudioContext, which node:test does not have (see engine-tick-exception-
// safety.test.mjs and pitch-fader-geometry.test.mjs for the same limit on the
// existing deck meters). The "no graph yet" branch is real production code,
// not a stub: peekMasterMeterReading must exist and must return the same
// silent-floor shape peekDeckMeterReading already returns before a graph
// exists.
// ---------------------------------------------------------------------------

test('peekMasterMeterReading exists and reads silence before any graph is built', () => {
	assert.equal(
		typeof audio.peekMasterMeterReading,
		'function',
		'audio-engine.svelte.ts must export peekMasterMeterReading, the master-bus counterpart of peekDeckMeterReading'
	);
	const reading = audio.peekMasterMeterReading();
	assert.equal(reading.db, meterTap.SILENT_METER_READING.db);
	assert.equal(reading.peakDb, meterTap.SILENT_METER_READING.db);
	assert.equal(reading.segments, 0);
	assert.equal(reading.normalized, 0);
	assert.equal(reading.clipped, false);
});

// ---------------------------------------------------------------------------
// masterMeterSource: real execution, not text matching (finding 2b fix).
//
// The old version of this test grepped the raw .ts source for one literal
// spelling of an object-literal push, e.g.
//   meterSources.push({ tap: _masterMeterTap, source: _masterGain })
// which executes nothing, goes red on a whitespace/ordering-only refactor,
// and goes green if that text exists anywhere at all (dead code, a comment)
// with nothing tying the match to the real meterSources array. The fix is
// the same one PR #1560 landed against AGENTS.md's "no mocks and locked real
// fixtures" rule for a different file: extract the step into a small pure,
// directly-callable function and call the REAL thing.
// ---------------------------------------------------------------------------

describe('createMasterMeterSource owns the master tap and its meterSources entry', () => {
	it('is exported as a real function, not inlined only in audio-engine', () => {
		assert.equal(typeof meterTap.createMasterMeterSource, 'function');
	});

	it('returns {tap, source} carrying the exact node handed to it', () => {
		const stubGainNode = { __stub: 'not a real AudioNode, and that is the point' };
		const result = meterTap.createMasterMeterSource(stubGainNode);
		assert.equal(
			result.source,
			stubGainNode,
			'must carry the exact node handed to it, not some other node'
		);
		assert.equal(typeof result.tap, 'object');
		assert.equal(result.tap.node, null, 'a fresh tap has no worklet node until attach');
		meterTap.releaseMasterMeterTap();
	});

	it('reads silence before creation and after release, and a real reading in between', () => {
		meterTap.releaseMasterMeterTap();
		assert.deepEqual(meterTap.masterMeterReading(0), meterTap.SILENT_METER_READING);

		meterTap.createMasterMeterSource({});
		const live = meterTap.masterMeterReading(1);
		assert.notEqual(
			live,
			meterTap.SILENT_METER_READING,
			'once a tap exists the reading must come from readMeterTap, not the fallback'
		);
		assert.equal(live.db, meterTap.SILENT_METER_READING.db, 'a tap with no observation yet floors');

		meterTap.releaseMasterMeterTap();
		assert.deepEqual(meterTap.masterMeterReading(2), meterTap.SILENT_METER_READING);
	});

	it('SILENT_METER_READING is frozen, so no consumer can mutate every meter at once', () => {
		assert.ok(Object.isFrozen(meterTap.SILENT_METER_READING));
	});
});

describe('master meter taps the master bus post master-gain, reusing meter-tap.ts', () => {
	const engineSource = readSrc('lib/rb/audio-engine.svelte.ts');

	it('creates one master tap through the same createMeterTap used for decks', () => {
		assert.match(engineSource, /meterSources\.push\(createMasterMeterSource\(_masterGain\)\)/);
	});

	it('releases the master tap on dispose, matching the _masterGain null contract', () => {
		// Anchored to the real, imported, unit-tested function call rather than
		// to the shape of an object literal a formatter is free to reorder.
		// The shape itself is proven by the "real execution" tests above; this
		// only needs to prove the ENGINE calls that real function with the
		// right arguments and pushes its result into meterSources.
		assert.match(engineSource, /releaseMasterMeterTap\(\)/);
	});

	it('imports the master-tap API from meter-tap.ts rather than reimplementing it', () => {
		assert.match(engineSource, /createMasterMeterSource[\s\S]{0,220}from '\$lib\/rb\/meter-tap'/);
		assert.doesNotMatch(engineSource, /let _masterMeterTap/);
	});

	it('does not invent a second meter-tap/meter-math pathway for the master bus', () => {
		// Only one createMeterTap import and one attachMeterTaps call site are
		// allowed to exist (armDeckMeters already carries both channel AND
		// master taps together, since they share one meterSources array).
		const attachCallSites = engineSource.match(/armDeckMeters\(/g) ?? [];
		assert.equal(
			attachCallSites.length,
			1,
			'a second attach call site would mean the master tap runs through a forked pathway'
		);
	});
});

// ---------------------------------------------------------------------------
// anyDeckPlaying: the RAF-gate predicate, real execution (finding 1 fix,
// Sol thread 3967718127).
//
// This is the one piece of the RAF-gating fix this suite CAN execute for
// real: node:test has no Svelte component runtime, so the $effect that
// starts/stops requestAnimationFrame from `active` cannot be mounted and
// observed here (same limit pitch-fader-geometry.test.mjs already documents
// for AudioContext-adjacent component behaviour). What CAN be proven for
// real is the shared predicate both the watchdog and TopBar's `active` gate
// now read from - deckStates is real reactive state, and this function
// reads it with no Svelte runtime required.
//
// TopBar's meter gate must come from playing-gate.ts's `anyDeckPlaying`
// (`playing || audible`), not from a second, weaker predicate. A previous
// version of this PR added a SECOND `anyDeckPlaying` directly to
// audio-engine.svelte.ts that read `playing` alone, shadowing the existing,
// stronger one playing-gate.ts already exported. That duplicate has been
// deleted; this suite pins TopBar to the surviving predicate and proves,
// by real execution, that the surviving predicate honours `audible` too -
// the exact gap Sol flagged (`_scheduleDeck` clears `playing` before the
// stop is presented, while `audible` still is).
// ---------------------------------------------------------------------------

describe('meterClockMs is the metering pair\'s own clock, not the engine\'s', () => {
	it('returns a finite, monotonic-enough millisecond reading', () => {
		const first = meterTap.meterClockMs();
		assert.equal(typeof first, 'number');
		assert.ok(Number.isFinite(first), `expected a finite ms reading, got ${first}`);
		assert.ok(meterTap.meterClockMs() >= first, 'the meter clock must not run backwards');
	});

	it('lives beside readMeterTap rather than in audio-engine, so the clock and its consumer share a module', () => {
		// It moved out of audio-engine.svelte.ts deliberately: readMeterTap
		// takes nowMs as a parameter precisely so the ballistics are testable
		// without faking a clock, so the default clock belongs in the same
		// module as the function whose parameter it fills.
		const engineSource = readSrc('lib/rb/audio-engine.svelte.ts');
		assert.doesNotMatch(engineSource, /function _meterClockMs/);
		assert.match(engineSource, /meterClockMs[\s\S]{0,120}from '\$lib\/rb\/meter-tap'/);
	});
});

describe('anyDeckPlaying is the single shared "is this session live" predicate', () => {
	it('audio-engine.svelte.ts does not export a second, weaker anyDeckPlaying', () => {
		// The one export must be gone entirely, not merely unused - a second
		// definition under the same name is exactly the duplicate-predicate
		// defect Sol's thread found, and it is a static regression to check.
		const engineSource = readSrc('lib/rb/audio-engine.svelte.ts');
		assert.doesNotMatch(
			engineSource,
			/export function anyDeckPlaying/,
			'audio-engine.svelte.ts must not define its own anyDeckPlaying - playing-gate.ts owns the one, ' +
				'stronger predicate'
		);
	});

	it('is exported as a real function from playing-gate.ts', () => {
		assert.equal(typeof masterMeterGate.anyDeckPlaying, 'function');
	});

	it('is false when no deck is playing, true the instant one is playing, false again after', () => {
		for (const deck of masterMeterGate.DECK_IDS) {
			assert.equal(masterMeterGate.deckStates[deck].playing, false);
		}
		assert.equal(masterMeterGate.anyDeckPlaying(), false);

		masterMeterGate.deckStates[2].playing = true;
		try {
			assert.equal(masterMeterGate.anyDeckPlaying(), true);
		} finally {
			masterMeterGate.deckStates[2].playing = false;
		}
		assert.equal(masterMeterGate.anyDeckPlaying(), false);
	});

	it('is also true while a deck is audible but no longer playing (finding A, Sol thread 3967718127)', () => {
		// _scheduleDeck clears `playing` before a stop is acknowledged or
		// presented, while `audible` can remain true through the scheduling
		// delay. A predicate that reads `playing` alone clears the master
		// meter while the master bus is still producing audio the listener
		// hears. Real execution, not a source-text match: flip `audible` with
		// `playing` false and prove the gate stays true.
		const deck = 2;
		assert.equal(masterMeterGate.deckStates[deck].playing, false);
		assert.equal(masterMeterGate.deckStates[deck].audible, false);
		assert.equal(masterMeterGate.anyDeckPlaying(), false);

		masterMeterGate.deckStates[deck].audible = true;
		try {
			assert.equal(
				masterMeterGate.anyDeckPlaying(),
				true,
				'audible alone (playing false) must still count as live'
			);
		} finally {
			masterMeterGate.deckStates[deck].audible = false;
		}
		assert.equal(masterMeterGate.anyDeckPlaying(), false);
	});

	it('the audio-context watchdog keeps its ORIGINAL inline playing-only predicate, unchanged', () => {
		// Deliberately NOT anyDeckPlaying (playing || audible). The watchdog's
		// arm predicate is a separate concern from the meter's visibility gate
		// and this PR must not change its semantics as a side effect of fixing
		// the meter gate - reverted back to the pre-PR inline check.
		//
		// #2155 (Sat 12 Sep 2026, commit 1db624ef7) added a third
		// `recreateGraph` argument to armAudioContextWatchdog for output-stall
		// recovery, so the call site now carries a trailing argument after the
		// predicate. The predicate itself is unchanged, which is what this
		// guard pins - the regex now tolerates that trailing argument instead
		// of requiring the call to end immediately after the predicate.
		const engineSource = readSrc('lib/rb/audio-engine.svelte.ts');
		assert.match(
			engineSource,
			/armAudioContextWatchdog\(\s*_ctx,\s*\(\)\s*=>\s*DECK_IDS\.some\(\(deck\)\s*=>\s*deckStates\[deck\]\.playing\)(?:,[^)]*)?\)/,
			'the watchdog arm predicate must be the original inline playing-only check, not the shared gate'
		);
	});
});

describe('MasterLevelMeter renders the shared ten-segment policy, not a new one', () => {
	const meter = readSrc('lib/components/rb/mixer/MasterLevelMeter.svelte');
	const topbar = readSrc('lib/components/rb/TopBar.svelte');

	it('imports segment thresholds and colour bands from meter-math, never redeclaring them', () => {
		assert.match(meter, /SEGMENT_THRESHOLDS_DBFS\.map/);
		assert.match(meter, /segmentBand\(index \+ 1\)/);
		assert.match(meter, /from '\$lib\/rb\/meter-math'/);
		for (const threshold of ['-34', '-26', '-20', '-16', '-12', '-9', '-6', '-3']) {
			assert.ok(
				!meter.includes(`${threshold},`),
				`MasterLevelMeter hardcodes the dB threshold ${threshold} instead of importing it`
			);
		}
	});

	it('reads through peekMasterMeterReading, never through a per-deck reading call', () => {
		assert.match(meter, /peekMasterMeterReading\(\)/);
		assert.doesNotMatch(meter, /peekDeckMeterReading/);
	});

	// Finding B (Sol thread 3967718149) gave `title` a second, unavailable-state
	// branch, so it is no longer a single flat `title={\`...\`}` template
	// literal - it is `title={label}`, `label` being a script-level $derived
	// whose FALSE (available) branch carries the exact prose these tests pin.
	// Anchoring to that $derived block, rather than to the whole file, keeps
	// the same anti-cheat property the old extractTemplateAttr anchor had: the
	// `<script>` docstring's prose must never be able to stand in for what the
	// rendered UI actually says.
	function extractLabelDerived(source) {
		const match = source.match(/const label = \$derived\(([\s\S]*?)\n\t\);/);
		assert.ok(match, 'expected a `const label = $derived(...)` block in the source');
		return match[1];
	}

	it('is honest about what red means here: digital headroom, not a speaker-damage guarantee', () => {
		// If the title/aria-label were deleted or replaced with something
		// dishonest, this must fail; it did not before, because it was reading
		// the docstring instead.
		const label = extractLabelDerived(meter);
		assert.doesNotMatch(label, /speaker damage|damage risk/i);
		// The real, present disclaimer in the shipped string - not "cannot
		// see", which only ever lived in the docstring.
		assert.match(label, /invisible from here/i);
		assert.match(label, /headroom/i);

		assert.match(meter, /aria-label=\{/, 'expected a computed aria-label on the meter');
		assert.doesNotMatch(meter, /aria-label=\{[\s\S]{0,400}?speaker damage|damage risk/i);
	});

	it('qualifies the "everything is summed" claim for externally-routed decks', () => {
		// Finding 3: a routed deck bypasses _masterGain and the crossfader
		// entirely (parseExternalRouting() in audio-engine.svelte.ts), so the
		// master meter can read near-silence while that deck is audibly
		// playing. The shipped title must not claim it reads every deck.
		const title = extractLabelDerived(meter);
		assert.match(
			title,
			/usb output|external rout|skip(s)? this bus/i,
			'title must name the external-routing exception, not just claim everything is summed'
		);
	});

	it('is wired into TopBar next to the existing master volume control', () => {
		assert.match(topbar, /import MasterLevelMeter from/);
		assert.match(topbar, /<MasterLevelMeter/);
	});
});

// ---------------------------------------------------------------------------
// RAF gating (finding 1): TopBar must not mount the meter always-on, and the
// component must not default to always-on either. Both are the fix, and
// both are real regression lines - the bug was exactly this pair (a `true`
// default component instantiated with no props at all) - so both sides are
// pinned rather than only one.
// ---------------------------------------------------------------------------

describe('the master meter RAF loop is gated on deck-playing state, not always-on', () => {
	const meter = readSrc('lib/components/rb/mixer/MasterLevelMeter.svelte');
	const topbar = readSrc('lib/components/rb/TopBar.svelte');

	it('defaults `active` to false, so an unwired instantiation cannot silently run forever', () => {
		assert.match(meter, /let\s*\{\s*active\s*=\s*false\s*\}\s*:\s*Props\s*=\s*\$props\(\)/);
	});

	it('TopBar passes an explicit active prop derived from deck-playing state, not a bare tag', () => {
		const tagMatch = topbar.match(/<MasterLevelMeter\b[^/]*\/>/);
		assert.ok(tagMatch, 'expected a self-closing <MasterLevelMeter ... /> tag in TopBar');
		assert.match(
			tagMatch[0],
			/active=\{masterMeterActive\}/,
			'TopBar must not mount <MasterLevelMeter /> with no props - that is the exact shape of the bug'
		);
		assert.match(
			topbar,
			/const masterMeterActive = \$derived\(anyDeckPlaying\(\)\)/,
			'the active prop must come from the shared anyDeckPlaying predicate, not a new ad hoc check'
		);
	});

	it('TopBar imports anyDeckPlaying from playing-gate.ts, the one module that owns that predicate', () => {
		// Not from audio-engine.svelte.ts: that file must never define a second
		// anyDeckPlaying (see the duplicate-predicate test above), so TopBar's
		// import edge is the fix's other half - it has to point at the real,
		// stronger predicate for the fix to matter.
		assert.match(topbar, /import\s*\{[^}]*anyDeckPlaying[^}]*\}\s*from\s*'\$lib\/rb\/playing-gate'/);
		assert.doesNotMatch(
			topbar,
			/import\s*\{[^}]*anyDeckPlaying[^}]*\}\s*from\s*'\$lib\/rb\/audio-engine\.svelte'/
		);
	});
});

// ---------------------------------------------------------------------------
// finding B (Sol thread 3967718149, BLOCKING): a broken meter must not look
// like a silent one.
//
// Before this fix, armDeckMeters's catch only wrote a perf-event row when
// addModule/AudioWorkletNode construction rejected. MasterLevelMeter then
// polled a tap that never receives an observation and rendered an ordinary
// `silent` reading forever - indistinguishable from a genuinely quiet master
// bus. AGENTS.md L244-L246 requires surfacing a terminal processor error
// rather than masking it.
// ---------------------------------------------------------------------------

describe('a failed meter arm is reported as unavailable, never as an ordinary silent reading', () => {
	it('meter-tap.ts exports the unavailable flag and its distinct reading as real functions/values', () => {
		assert.equal(typeof meterTap.markMetersUnavailable, 'function');
		assert.equal(typeof meterTap.metersUnavailable, 'function');
		assert.equal(typeof meterTap.UNAVAILABLE_METER_READING, 'object');
		assert.notEqual(
			meterTap.UNAVAILABLE_METER_READING,
			meterTap.SILENT_METER_READING,
			'unavailable must be a distinct object identity from silent, or a consumer cannot tell them apart'
		);
	});

	it('MasterLevelMeter learns of a failure without polling, and does not clear it while idle', () => {
		// finding E (Sol thread 3968599827): the graph is often armed while
		// nothing is playing, and the level RAF loop is deliberately gated off
		// then, so the verdict has to arrive by notification. Clearing it in the
		// inactive branch is what made a terminal failure look like silence.
		const meter = readSrc('lib/components/rb/mixer/MasterLevelMeter.svelte');
		assert.match(
			meter,
			/onMetersUnavailableChange\(/,
			'must subscribe to the verdict rather than depend on the gated RAF poll'
		);
		const inactiveBranch = meter.slice(meter.indexOf('if (!live)'), meter.indexOf('const tick'));
		assert.doesNotMatch(
			inactiveBranch,
			/unavailable\s*=\s*false/,
			'going idle must reset the LEVEL, never the failure verdict'
		);
	});

	it('MasterLevelMeter renders a visibly distinct unavailable state with an honest title/aria-label', () => {
		const meter = readSrc('lib/components/rb/mixer/MasterLevelMeter.svelte');
		assert.match(
			meter,
			/metersUnavailable[\s\S]{0,120}from '\$lib\/rb\/audio-engine\.svelte'/,
			'must read the real unavailable flag, not invent a local one'
		);
		assert.match(meter, /unavailable/, 'must carry an unavailable branch at all');
		assert.match(
			meter,
			/level unavailable - the meter failed to start/,
			'the honest, user-facing string this pin requires'
		);
		// Never claims silence when broken.
		assert.doesNotMatch(
			meter,
			/unavailable\s*\?\s*['"`]silent['"`]/,
			'must not fabricate a silent reading for the unavailable case'
		);
		// A distinct CSS hook proves the state is visibly different, not just an
		// aria string nobody sees (a sighted user watching the bar matters too).
		assert.match(meter, /class:unavailable/);
		assert.match(meter, /\.rb-master-level-meter\.unavailable/);
	});

	it('does not silently hide the meter in the unavailable state (still rendered, not conditionally removed)', () => {
		const meter = readSrc('lib/components/rb/mixer/MasterLevelMeter.svelte');
		// The meter's role="meter" element is unconditional (no {#if} gate around
		// the whole markup keyed on `unavailable`) - hiding it would be exactly
		// as dishonest as showing a fabricated silent reading.
		assert.doesNotMatch(meter, /\{#if\s+!?unavailable/);
	});
});
