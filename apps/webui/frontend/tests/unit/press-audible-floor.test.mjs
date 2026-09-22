// requirement: LATENCY-03
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import {
	engineBlockAfter,
	readFrontendSource as readSource,
	SCHEDULE_DECK_SERIAL_ANCHOR
} from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Q1 / LATENCY-03: the device floor a press-to-audible row may claim.
 *
 * `input_to_audible_ms` models the SCHEDULED START. The instant a DJ actually
 * hears the sound is later by the output device's own floor, and the floor is
 * where this instrument could most easily lie, because of a platform fact
 * measured on Wed 9 Sep 2026 rather than assumed:
 *
 *   engine   baseLatency   outputLatency fresh   outputLatency running
 *   WebKit   2.902ms       0                     15.964ms
 *   Chromium 5.805ms       0                     32.000ms
 *
 * `outputLatency` is not missing on WKWebView. It is present, finite and typed
 * `number`, and it reads ZERO until the context has rendered - so every guard
 * asking "can I read this?" says yes and the row reports a device floor of
 * nothing. The first play of a session is exactly such a schedule, because
 * `_resumeContext()` runs on that press.
 *
 * Regression lines:
 * - if a zero device floor is treated as a measurement then the press latency
 *   is understated by ~16ms on the shipped engine and nothing in the row says so
 * - if input_to_output_ms is emitted from a partial floor then the budget
 *   number is wrong in the flattering direction, which is the one direction it
 *   may never fail in
 * - if the row carries no latency_floor label then a reader cannot tell a
 *   complete floor from a row that quietly dropped a term
 * - if press rows share the pitch fader's ring bucket then one drag evicts the
 *   number the whole program turns on
 */

let floor;
let math;

before(async () => {
	floor = await loadTypeScriptModule('src/lib/player/transport/press-audible.ts');
	math = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
});

//-----------------------------------------------------------------------------
// the availability rule
//-----------------------------------------------------------------------------

test('a device floor of exactly zero is the absence of a measurement, not one', () => {
	// The invariant, stated as an invariant rather than as a platform table: no
	// real output path has zero latency, so a zero is always "not filled in yet".
	assert.equal(floor.isMeasuredLatencyFloor(0), false);
	assert.equal(floor.isMeasuredLatencyFloor(-0), false);
	assert.equal(floor.isMeasuredLatencyFloor(undefined), false);
	assert.equal(floor.isMeasuredLatencyFloor(Number.NaN), false);
	assert.equal(floor.isMeasuredLatencyFloor(Number.POSITIVE_INFINITY), false);
	// The real WebKit and Chromium running readings.
	assert.equal(floor.isMeasuredLatencyFloor(0.01596371882086168), true);
	assert.equal(floor.isMeasuredLatencyFloor(0.032), true);
});

test('the unavailable terms are NAMED, so a row says which number it lost', () => {
	// The observed WKWebView pre-render state: base is real, output is a zero.
	assert.deepEqual(
		floor.unavailableLatencyTerms({
			base: 0.0029024943310657597,
			output: 0
		}),
		['output_latency_ms']
	);
	assert.deepEqual(
		floor.unavailableLatencyTerms({ base: 0.0029, output: 0.01596 }),
		[]
	);
	assert.deepEqual(
		floor.unavailableLatencyTerms({ base: undefined, output: undefined }),
		['base_latency_ms', 'output_latency_ms']
	);
});

test('the labels state completeness explicitly, never by omission', () => {
	const complete = floor.latencyFloorLabels({ unavailable: [], contextState: 'running' });
	assert.equal(complete.latency_floor, 'complete');
	assert.equal(complete.audio_context_state, 'running');
	assert.equal(complete.latency_unavailable, undefined);

	const partial = floor.latencyFloorLabels({
		unavailable: ['output_latency_ms'],
		contextState: 'suspended'
	});
	assert.equal(partial.latency_floor, 'partial');
	assert.equal(partial.latency_unavailable, 'output_latency_ms');
	// The usual REASON, so a pre-render zero is distinguishable from a dead device.
	assert.equal(partial.audio_context_state, 'suspended');
});

test('latency_floor is present on BOTH branches, so absence means an old row', () => {
	// A label that only appears when something is wrong cannot be told apart
	// from a row written before the field existed.
	for (const unavailable of [[], ['output_latency_ms']]) {
		const labels = floor.latencyFloorLabels({ unavailable, contextState: 'running' });
		assert.ok('latency_floor' in labels);
		assert.equal(typeof labels.latency_floor, 'string');
	}
});

//-----------------------------------------------------------------------------
// the audible-instant arithmetic, computed by hand
//-----------------------------------------------------------------------------

test('input_to_output_ms is the press carried all the way out of the device', () => {
	// Hand-computed against the real WebKit running floor:
	//   press wait          12.5   ms
	//   scheduled offset    54.8   ms
	//   baseLatency          2.902 ms  (0.0029024943310657597 s)
	//   outputLatency       15.964 ms  (0.01596371882086168 s)
	//   total               86.166 ms
	const ms = floor.inputToOutputMs({
		pressToScheduleMs: 12.5,
		scheduledOffsetMs: 54.8,
		baseLatencySec: 0.0029024943310657597,
		outputLatencySec: 0.01596371882086168
	});
	assert.equal(Math.round(ms * 1000) / 1000, 86.166);
	// And it is strictly later than the scheduled-start model, by the floor.
	assert.ok(ms > 12.5 + 54.8);
	assert.equal(Math.round((ms - (12.5 + 54.8)) * 1000) / 1000, 18.866);
});

test('a partial floor withholds the total rather than shrinking it', () => {
	// The whole refusal. A press-to-output figure with the output stage dropped
	// is not a smaller number, it is a wrong one.
	assert.equal(
		floor.inputToOutputMs({
			pressToScheduleMs: 12.5,
			scheduledOffsetMs: 54.8,
			baseLatencySec: 0.0029024943310657597,
			outputLatencySec: 0
		}),
		undefined
	);
	assert.equal(
		floor.inputToOutputMs({
			pressToScheduleMs: 12.5,
			scheduledOffsetMs: 54.8,
			baseLatencySec: undefined,
			outputLatencySec: 0.016
		}),
		undefined
	);
	// No press behind the schedule means no press number at all.
	assert.equal(
		floor.inputToOutputMs({
			pressToScheduleMs: undefined,
			scheduledOffsetMs: 54.8,
			baseLatencySec: 0.0029,
			outputLatencySec: 0.016
		}),
		undefined
	);
});

test('SABOTAGE: the total moves with EVERY one of its four terms', () => {
	// A total insensitive to a term is a total that is not really summing it.
	const base = {
		pressToScheduleMs: 12.5,
		scheduledOffsetMs: 54.8,
		baseLatencySec: 0.003,
		outputLatencySec: 0.016
	};
	const baseline = floor.inputToOutputMs(base);
	const moves = [
		['pressToScheduleMs', { ...base, pressToScheduleMs: 112.5 }, 100],
		['scheduledOffsetMs', { ...base, scheduledOffsetMs: 154.8 }, 100],
		['baseLatencySec', { ...base, baseLatencySec: 0.103 }, 100],
		['outputLatencySec', { ...base, outputLatencySec: 0.116 }, 100]
	];
	for (const [name, input, expectedDeltaMs] of moves) {
		const moved = floor.inputToOutputMs(input);
		assert.equal(
			Math.round((moved - baseline) * 1000) / 1000,
			expectedDeltaMs,
			`a ${expectedDeltaMs}ms move in ${name} must move input_to_output_ms by the same`
		);
	}
});

//-----------------------------------------------------------------------------
// the stages row this all lands in
//-----------------------------------------------------------------------------

/** The shipping shape, with the real WebKit running device floor. */
function stages(overrides = {}) {
	return math.scheduleOffsetStages({
		contextTimeSec: 10,
		requestedWhenSec: 10.0548,
		effectiveWhenSec: 10.0548,
		processorLeadSec: 0.0468,
		processorLatencySec: 0.12,
		baseLatencySec: 0.0029024943310657597,
		outputLatencySec: 0.01596371882086168,
		active: true,
		...overrides
	});
}

test('an unmeasured floor term is ABSENT from the stages, never a zero', () => {
	const row = stages({ outputLatencySec: 0, pressToScheduleMs: 12.5 });
	assert.equal(
		row.output_latency_ms,
		undefined,
		'a zero here is indistinguishable from a real reading of zero, and understates ' +
			'the floor by ~16ms on the shipped engine'
	);
	assert.equal(row.base_latency_ms, 2.902, 'the term that WAS measured must survive');
	assert.equal(
		row.input_to_output_ms,
		undefined,
		'and the total that depends on it must be withheld, not shrunk'
	);
	// The half that never depended on the floor is unharmed.
	assert.equal(row.input_to_audible_ms, 67.3);
});

test('a complete floor earns every key, and the JSON round trip survives', () => {
	const row = stages({ pressToScheduleMs: 12.5 });
	assert.equal(row.base_latency_ms, 2.902);
	assert.equal(row.output_latency_ms, 15.964);
	assert.equal(row.input_to_audible_ms, 67.3);
	assert.equal(row.input_to_output_ms, 86.166);
	for (const [name, value] of Object.entries(row)) {
		assert.equal(typeof value, 'number', `${name} must be a number for the stages contract`);
		assert.ok(Number.isFinite(value), `${name} must be finite, got ${value}`);
	}
	assert.deepEqual(JSON.parse(JSON.stringify(row)), row);
});

test('an undefined floor no longer throws the transport path over', () => {
	// It used to: the finite check covered every field, so a platform without
	// the property would have taken RangeError on the way to processor.schedule
	// and broken audio outright. An instrument may never break what it measures.
	assert.doesNotThrow(() =>
		stages({ baseLatencySec: undefined, outputLatencySec: undefined, pressToScheduleMs: 5 })
	);
	const row = stages({
		baseLatencySec: undefined,
		outputLatencySec: undefined,
		pressToScheduleMs: 5
	});
	assert.equal(row.base_latency_ms, undefined);
	assert.equal(row.output_latency_ms, undefined);
	assert.equal(row.input_to_output_ms, undefined);
	assert.equal(row.scheduled_offset_ms, 54.8, 'the pre-existing ratchet number is untouched');
});

//-----------------------------------------------------------------------------
// the ring bucket
//-----------------------------------------------------------------------------

test('a pressed schedule files under its OWN kind, a suffix of the plain one', () => {
	assert.equal(floor.scheduleRowKind(undefined, false), 'transport-schedule');
	assert.equal(floor.scheduleRowKind(0, false), 'transport-schedule-press');
	assert.equal(floor.scheduleRowKind(12.5, false), 'transport-schedule-press');
	// The suffix shape is what keeps every existing prefix-matching consumer
	// (the probe mirror, the e2e console grep) working unchanged.
	assert.ok(floor.PRESS_SCHEDULE_KIND.startsWith(floor.SCHEDULE_KIND));
});

//-----------------------------------------------------------------------------
// P1 BLOCKING r3974057968: the load-spanning classification
//-----------------------------------------------------------------------------

test('a load-spanning press files under its OWN kind, a suffix of the press one', () => {
	// No press behind the schedule: loadSpanning can never override that,
	// or a pitch-fader drag with a stale flag set would masquerade as a
	// press measurement.
	assert.equal(floor.scheduleRowKind(undefined, true), 'transport-schedule');
	assert.equal(floor.scheduleRowKind(12.5, true), 'transport-schedule-press-load-span');
	assert.equal(floor.scheduleRowKind(12.5, false), 'transport-schedule-press');
	// The suffix chain is what keeps _bucketOf's startsWith('transport-schedule-press')
	// check routing these into the SAME ring budget as an ordinary press row,
	// while still being the longest kind an S2 p95 consumer can match first.
	assert.ok(floor.PRESS_SCHEDULE_LOAD_SPAN_KIND.startsWith(floor.PRESS_SCHEDULE_KIND));
	assert.ok(floor.PRESS_SCHEDULE_LOAD_SPAN_KIND.startsWith(floor.SCHEDULE_KIND));
});

//-----------------------------------------------------------------------------
// P1 BLOCKING PRRT_kwDOSEvNd86g96Le: the armed hot-cue classification
//-----------------------------------------------------------------------------

test('an armed hot-cue press files under its OWN kind, a suffix of the press one', () => {
	// No press behind the schedule: armed can never override that, same
	// invariant as loadSpanning above.
	assert.equal(floor.scheduleRowKind(undefined, false, true), 'transport-schedule');
	assert.equal(floor.scheduleRowKind(12.5, false, true), 'transport-schedule-press-armed');
	assert.equal(floor.scheduleRowKind(12.5, false, false), 'transport-schedule-press');
	// Load-spanning takes priority over armed - the two conditions describe
	// different presses in this codebase, but if a caller ever passed both,
	// the wait spanning a whole deck load is the more informative label.
	assert.equal(floor.scheduleRowKind(12.5, true, true), 'transport-schedule-press-load-span');
	// The suffix chain keeps _bucketOf's startsWith('transport-schedule-press')
	// routing these into the press row's own ring budget, while still being a
	// kind an S2 p95/p99 consumer can exclude before reading Class A latency.
	assert.ok(floor.PRESS_SCHEDULE_ARMED_KIND.startsWith(floor.PRESS_SCHEDULE_KIND));
	assert.ok(floor.PRESS_SCHEDULE_ARMED_KIND.startsWith(floor.SCHEDULE_KIND));
});

test('SABOTAGE: one pitch-fader drag can no longer evict the press row', async () => {
	// The exact gesture: start a track, then reach for the pitch fader to
	// beatmatch it. PitchFader drives _scheduleDeck from an unthrottled
	// pointermove, so the drag is ~40 rows against a 16-row budget - which used
	// to take the press row with it, about a second after it was written.
	const perfLog = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
	perfLog.recordPerfTiming(
		floor.PRESS_SCHEDULE_KIND,
		{ input_to_audible_ms: 67.3, input_to_output_ms: 86.166 },
		1
	);
	for (let i = 0; i < 40; i++) {
		perfLog.recordPerfTiming(floor.SCHEDULE_KIND, { scheduled_offset_ms: 8 + i }, 1);
	}
	const rows = perfLog.readPerfEvents();
	const press = rows.filter((row) => row.kind === floor.PRESS_SCHEDULE_KIND);
	assert.equal(press.length, 1, 'the press row must survive a full fader drag');
	assert.equal(press[0].stages.input_to_audible_ms, 67.3);
	// And the control that makes this a measurement: the drag really did flood.
	assert.equal(
		rows.filter((row) => row.kind === floor.SCHEDULE_KIND).length,
		16,
		'if the fader rows did not hit their own budget then nothing was under pressure ' +
			'and the survival above proves nothing'
	);
});

//-----------------------------------------------------------------------------
// wiring
//-----------------------------------------------------------------------------

test('the engine labels the row from the SAME context it read the clock from', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_SERIAL_ANCHOR);
	// The facts are DERIVED FROM scheduleStages, so the floor terms the labels
	// call absent are the same ones the row itself omits. A second read of the
	// context could disagree with the row printed beside it.
	assert.ok(
		body.includes('const row = scheduleRowFacts(scheduleStages, _ctx.state, pressT0Ms);'),
		'the labels must come from the row being filed, and carry the context state ' +
			'read at that same point; without it a reader cannot tell a pre-render ' +
			'zero from a dead output device'
	);
	// Ordering is the property, not just presence: the read has to happen
	// BEFORE the worklet acknowledgement, because the context can move from
	// suspended to running while that await is in flight.
	assert.ok(
		body.indexOf('const row = scheduleRowFacts(') < body.indexOf('await processor.schedule'),
		'a floor read after the ack describes a later context than the row it labels'
	);
	assert.ok(
		body.includes('recordPerfTiming(row.kind, scheduleStages, deck, row.labels)'),
		'the labels must actually reach the ring, not merely be computed'
	);
});

test('every P0 press path threads the DOM event stamp, none re-takes the clock', () => {
	// The gap this closes: runPerformanceCommandFromUi defaults the stamp to its
	// own performance.now(), which is already downstream of the browser input
	// queue and of handler dispatch. Before Q1's wiring, NOT ONE call site in
	// the app passed a real event stamp, so that wait was invisible on every
	// press despite the parameter existing to carry it.
	const cluster = readSource('src/lib/components/rb/deck/TransportCluster.svelte');
	assert.ok(
		// c8d78c993 (fix: LATENCY-02 QUANTIZED LAUNCH gestures, armed glyph, and master
		// clock) added a quantize arg after the stamp; the stamp is still the first arg.
		/await onPlayPause\(event\.timeStamp, event\.metaKey \|\| event\.ctrlKey\)\}/.test(cluster),
		'the play button must hand on its own event stamp'
	);
	assert.ok(
		cluster.includes('onclick={async (event) => await onCue(event.timeStamp)}'),
		'and so must cue'
	);

	const deck = readSource('src/lib/components/rb/Deck.svelte');
	assert.ok(
		// c8d78c993 (LATENCY-02 QUANTIZED LAUNCH) appended a quantize param after the stamp.
		deck.includes('async function playPause(pressT0Ms?: number, quantize?: boolean)') &&
			deck.includes('async function returnToCue(pressT0Ms?: number)'),
		'the deck handlers must accept the stamp rather than dropping it on the floor'
	);
	assert.ok(
		deck.includes("{ type: 'cue', deck: deckId }, pressT0Ms"),
		'and forward it into the command, or the parameter is decorative'
	);

	const hotkeys = readSource('src/lib/rb/performance-hotkeys.ts');
	assert.ok(
		hotkeys.includes('void _toggleRecentPlay(e.timeStamp);'),
		'Space is how a DJ starts a track without looking at the screen; it is the LAST ' +
			'path that should be measuring from after its own dispatch'
	);
	assert.ok(
		// c8d78c993 (LATENCY-02 QUANTIZED LAUNCH) split the play literal across lines to
		// carry an optional quantize flag; the keydown stamp must still be the second arg.
		/const playing = !st\.playing;\s*await runPerformanceCommandFromUi\(\s*\{\s*type: 'play',\s*deck,\s*playing,\s*\.\.\.\(quantize === true && playing \? \{ quantize: true \} : \{\}\)\s*\},\s*pressT0Ms\s*\)/.test(
			hotkeys
		),
		'the keydown stamp must reach the command'
	);

	// Hot-cue: a Class A transport control budgeted like play/cue, but with
	// TWO independent entry points (a populated pad's UI click, a controller's
	// MIDI note) into the SAME hot_cue_trigger command - both must carry the
	// stamp, or the deck buttons/Space check above passes while this one
	// silently does not.
	const bank = readSource('src/lib/components/rb/deck/HotCueBank.svelte');
	assert.ok(
		bank.includes('onJump: (slot: HotCueSlot, pressT0Ms?: number) => Promise<void>;'),
		'HotCueBank must declare its onJump callback wide enough to carry a stamp'
	);
	assert.ok(
		bank.includes('onclick={(e) => onSlotClick(entry, e.timeStamp)}'),
		'and the pad click must hand on its own event stamp'
	);
	assert.ok(
		/await onJump\(entry\.slot, pressT0Ms\);/.test(bank),
		'onSlotClick must forward the stamp into onJump, or the parameter is decorative'
	);

	assert.ok(
		deck.includes(
			'async function triggerHotCue(slot: HotCueSlot, pressT0Ms?: number): Promise<void> {'
		),
		'Deck.svelte must accept the stamp rather than dropping it on the floor'
	);
	assert.ok(
		deck.includes("{ type: 'hot_cue_trigger', deck: deckId, slot }, pressT0Ms"),
		'and forward it into the command'
	);

	const glue = readSource('src/lib/rb/midi/action-glue.svelte.ts');
	assert.ok(
		glue.includes('function _cmdHotCue(deck: DeckId, slot: HotCueSlot, pressT0Ms?: number): void {'),
		'the MIDI hot-cue adapter must accept the receipt stamp like _cmdPlayToggle/_cmdPressCue do'
	);
	assert.ok(
		glue.includes("{ type: 'hot_cue_trigger', deck, slot }, pressT0Ms"),
		'and forward it into the command'
	);
	assert.ok(
		glue.includes('_cmdHotCue(action.deck, action.slot, pressT0Ms);'),
		'the deck_hot_cue case must pass its own already-available pressT0Ms, not drop it'
	);

	// The command still has to reach the audio clock: performance-ipc's
	// hot_cue_trigger executor forwards its OWN pressT0Ms parameter into both
	// branches of the jump/arm driver, and the engine's quantizedSeek /
	// armHotCueTrigger both carry it into _scheduleDeck - otherwise every
	// layer above this one is decorative.
	const ipc = readSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.ok(
		/await _hotCueDriver\.jump\(command\.deck, cue\.in_ms, pressT0Ms\);/.test(ipc),
		'the immediate-jump branch must forward pressT0Ms into the driver'
	);
	assert.ok(
		/_hotCueDriver\.arm\(\s*command\.deck,\s*cue\.in_ms,\s*plan\.armAtPositionSec,\s*pressT0Ms\s*\);/.test(ipc),
		'the arm-for-downbeat branch must forward pressT0Ms into the driver'
	);

	const engine = readSource('src/lib/rb/audio-engine.svelte.ts');
	assert.ok(
		/async quantizedSeek\(\s*deck: DeckId,\s*ms: number,\s*skipGridQuantize = false,\s*pressT0Ms\?: number\s*\): Promise<void> \{/.test(
			engine
		),
		'quantizedSeek must accept the stamp rather than dropping it on the floor'
	);
	assert.ok(
		/async armHotCueTrigger\(\s*deck: DeckId,\s*targetPositionMs: number,\s*armAtPositionSec: number,\s*pressT0Ms\?: number\s*\): Promise<number> \{/.test(
			engine
		),
		'armHotCueTrigger must accept the stamp rather than dropping it on the floor'
	);
});

test('NEGATIVE CONTROL: a press on an empty deck dispatches nothing at all', () => {
	// If silence can produce a latency row then the instrument is measuring
	// something other than sound starting. The guard is a return, not a catch:
	// no command leaves the hotkey, so no schedule happens, so no press row can
	// be written for a deck that has nothing to play.
	const hotkeys = readSource('src/lib/rb/performance-hotkeys.ts');
	const body = hotkeys.slice(hotkeys.indexOf('async function _toggleRecentPlay('));
	const guardAt = body.indexOf('if (st.stable_id === null) return;');
	// c8d78c993 (LATENCY-02 QUANTIZED LAUNCH) made the play literal multi-line. The
	// earlier load_play_intent dispatch (6d4762389) targets a deck mid-load, not an
	// empty one, so the ordering is pinned against the play dispatch specifically.
	const dispatchAt = body.search(/runPerformanceCommandFromUi\(\s*\{\s*type: 'play',/);
	assert.ok(guardAt !== -1, 'the empty-deck guard must exist');
	assert.ok(dispatchAt !== -1, 'the play dispatch must exist to order the guard against');
	assert.ok(
		guardAt < dispatchAt,
		'and must return BEFORE the play command, or an empty deck reaches the engine and ' +
			'the row it produces is a latency figure for silence'
	);
});

test('scheduleRowFacts reads the row it labels, not a second opinion of the context', async () => {
	const press = await loadTypeScriptModule('src/lib/rb/press-stamp.ts');

	// A complete floor: both terms present in the stages, and a press behind it.
	const complete = press.scheduleRowFacts(
		{ press_to_schedule_ms: 12.5, base_latency_ms: 2.902, output_latency_ms: 15.964 },
		'running'
	);
	assert.equal(complete.kind, 'transport-schedule-press');
	assert.equal(complete.labels.latency_floor, 'complete');
	assert.equal(complete.labels.audio_context_state, 'running');
	assert.equal(complete.labels.latency_unavailable, undefined);

	// The pre-render case: scheduleOffsetStages OMITS output_latency_ms, so the
	// absence read here is the same absence the row reports.
	const partial = press.scheduleRowFacts(
		{ press_to_schedule_ms: 12.5, base_latency_ms: 2.902 },
		'suspended'
	);
	assert.equal(partial.labels.latency_floor, 'partial');
	assert.equal(partial.labels.latency_unavailable, 'output_latency_ms');

	// No press behind the schedule keeps the row on the plain kind, so a pitch
	// fader drag cannot masquerade as a press measurement.
	const nopress = press.scheduleRowFacts({ base_latency_ms: 2.902, output_latency_ms: 15.964 }, 'running');
	assert.equal(nopress.kind, 'transport-schedule');

	// SABOTAGE: a Chromium-shaped row -- context says running, floor is not
	// filled in. The state must NOT be allowed to vouch for the floor.
	const chromiumFresh = press.scheduleRowFacts({ press_to_schedule_ms: 12.5, base_latency_ms: 5.805 }, 'running');
	assert.equal(chromiumFresh.labels.latency_floor, 'partial');
	assert.equal(chromiumFresh.labels.audio_context_state, 'running');
});

test('scheduleRowFacts classifies an UNMARKED stamp as plain-press, and tolerates no stamp at all', async () => {
	const press = await loadTypeScriptModule('src/lib/rb/press-stamp.ts');

	// A stamp nobody marked (the ordinary DOM/MIDI press path) stays on the
	// plain press kind - claimLoadSpanningPress must answer false for it.
	const ordinary = press.scheduleRowFacts({ press_to_schedule_ms: 8.2 }, 'running', 98765.4);
	assert.equal(ordinary.kind, 'transport-schedule-press');

	// No pressT0Ms at all (e.g. the two-arg call sites earlier in this file)
	// must not throw and must behave exactly as before this fix.
	const noStamp = press.scheduleRowFacts({ press_to_schedule_ms: 8.2 }, 'running');
	assert.equal(noStamp.kind, 'transport-schedule-press');
});

test('press-stamp.ts actually spends claimLoadSpanningPress into the row kind', () => {
	// scheduleRowFacts and deck-slots.ts's registry are exercised separately
	// above (each `loadTypeScriptModule` call is its own esbuild bundle, so a
	// mark made through a directly-loaded deck-slots.ts instance is invisible
	// to a SEPARATELY bundled press-stamp.ts - only the real Vite module graph
	// shares one instance). The wiring between them is source-verified here,
	// the same way this file already verifies audio-engine.svelte.ts's call
	// shape above.
	const src = readSource('src/lib/rb/press-stamp.ts');
	assert.ok(
		/import \{[\s\S]*?claimArmedHotCuePress[\s\S]*?claimLoadSpanningPress[\s\S]*?\} from '\$lib\/rb\/deck-slots';/.test(
			src
		),
		'the classification must come from the registry deck-slots.ts owns'
	);
	assert.ok(
		/kind: scheduleRowKind\(\s*stages\.press_to_schedule_ms,\s*claimLoadSpanningPress\(pressT0Ms\),\s*claimArmedHotCuePress\(pressT0Ms\)\s*\),/.test(
			src
		),
		'both claim results must actually reach scheduleRowKind, not merely be computed'
	);
});

//-----------------------------------------------------------------------------
// the three press paths review found unstamped (#1657 r3973957845/49, r3974057968)
//-----------------------------------------------------------------------------

test('a Beat Sync follower start carries the press that caused it', () => {
	// The normal two-deck gesture: deck B is started while deck A is master
	// and Beat Sync is on, so play() routes through _synchronizeFollowers
	// instead of _schedulePress. That branch supplied no stamp, so the most
	// common start in a real set produced a PLAIN row and never appeared in
	// the headline number.
	const body = readSource('src/lib/rb/audio-engine.svelte.ts');
	// 0a8631e82 (fix: unlock and resume locked paused Beat Sync MASTER, DECKUX-17) and
	// c8d78c993 (LATENCY-02 master clock) replaced the single activeMaster branch with
	// several master-selection branches (syncClock, elected). EVERY follower start
	// inside play() must forward the stamp, not merely one of them.
	const playBody = body.slice(
		body.indexOf('async play(deck: DeckId, pressT0Ms?: number'),
		body.indexOf('async pause(deck: DeckId, pressT0Ms?: number)')
	);
	const followerStarts = playBody.match(/await _synchronizeFollowers\([^)]*\[deck\], \{[\s\S]*?\}\);/g) ?? [];
	assert.ok(followerStarts.length > 0, 'play() must still have a Beat Sync follower branch');
	for (const call of followerStarts) {
		assert.ok(
			call.includes('...(pressT0Ms === undefined ? {} : { pressT0Ms })'),
			`the sync branch of play() must forward the press it was given: ${call}`
		);
	}
	assert.ok(
		/return _scheduleDeck\(\s*item\.deck,[\s\S]*?options\.pressT0Ms\s*\);/.test(body),
		'and the follower schedule must actually spend it, not just receive it'
	);
	// NEGATIVE CONTROL: the resync callers must NOT stamp, or a background
	// re-anchor nobody pressed would file a press row.
	assert.ok(
		!body.includes('_synchronizeFollowers(master, followers, { pressT0Ms'),
		'a background re-anchor must never file a press row'
	);
});

test('a hot-cue jump against an already-playing synced follower carries the press that caused it', () => {
	// quantizedSeek's 'follower' and 'master-max' branches are BOTH re-anchor
	// paths (deck already playing, already beat-synced) - PRRT_kwDOSEvNd86g96Li
	// found the stamp `quantizedSeek` was newly given getting dropped before
	// either branch's `_synchronizeFollowers` call.
	const body = readSource('src/lib/rb/audio-engine.svelte.ts');
	assert.ok(
		/await _synchronizeFollowers\(syncPlan\.master, \[deck\], \{\s*followerAnchorSec: \{ \[deck\]: targetMs \/ 1000 \},\s*reanchorDecks: new Set\(\[deck\]\),\s*\.\.\.\(pressT0Ms === undefined \? \{\} : \{ pressT0Ms \}\)/.test(
			body
		),
		"'follower' must forward the press it was given"
	);
	assert.ok(
		/reanchorDecks: new Set\(syncPlan\.followers\),\s*\.\.\.\(pressT0Ms === undefined \? \{\} : \{ pressT0Ms \}\)/.test(
			body
		),
		"'master-max' must forward the press it was given"
	);
	// And the re-anchor/blend helpers those branches call must actually SPEND
	// it, not just receive it - both funnel through the shared _scheduleSyncDeck.
	// Anchored on the two facts, not on the parameter ORDER: #3653 added a
	// `reanchorGeneration` argument after `pressT0Ms` in both signatures, which
	// a regex requiring `pressT0Ms` to be last read as the stamp having been
	// dropped. What must hold is that the helper takes the stamp and hands it
	// to `_scheduleDeck`.
	const syncDeck = body.slice(
		body.indexOf('function _scheduleSyncDeck('),
		body.indexOf('\n}', body.indexOf('function _scheduleSyncDeck(')) + 2
	);
	assert.ok(
		syncDeck.startsWith('function _scheduleSyncDeck('),
		'_scheduleSyncDeck must exist for this guard to pin anything'
	);
	assert.ok(
		/\bpressT0Ms\?: number\b/.test(syncDeck),
		'_scheduleSyncDeck must still receive the press stamp'
	);
	assert.ok(
		/return _scheduleDeck\([^;]*\bpressT0Ms\b[^;]*\);/.test(syncDeck),
		'_scheduleSyncDeck must spend the stamp it was given, not just receive it'
	);
	// ...and in the press slot specifically. Both parameters take a number, so
	// `undefined, pressT0Ms` would send the stamp as reanchorGeneration, drop
	// it from the press slot, and leave the assertion above green with nothing
	// type checking would catch (P2 r4055758675). Pinned RELATIVE to the
	// generation argument rather than by absolute position, so #3653's next
	// equivalent -- another argument appended -- does not read as a regression.
	const forwarded = syncDeck.slice(
		syncDeck.indexOf('_scheduleDeck('),
		syncDeck.indexOf(');', syncDeck.indexOf('_scheduleDeck('))
	);
	const pressAt = forwarded.indexOf('pressT0Ms');
	const generationAt = forwarded.indexOf('reanchorGeneration');
	assert.ok(
		generationAt >= 0,
		'reanchorGeneration must still be forwarded for this ordering guard to mean anything'
	);
	assert.ok(
		pressAt >= 0 && pressAt < generationAt,
		'the press stamp must occupy the press slot, ahead of reanchorGeneration, ' +
			'not be swapped into the generation slot: ' +
			forwarded.replace(/\s+/g, ' ')
	);
	// NEGATIVE CONTROL: 'master-max' fills reanchorDecks with the OTHER
	// followers, never the pressed master - a re-anchored bystander must not
	// inherit the operator's press just because it shares the same sync call.
	assert.ok(
		body.includes('options.masterSchedule === undefined ? options.pressT0Ms : undefined'),
		'a master-max bystander follower must not file the pressed deck\'s row'
	);
});

test('a play deferred by a load times from the keydown, not from the load', () => {
	// Space on a still-decoding track is a supported gesture. The stamp has to
	// survive the load, or the row starts timing after it and understates the
	// felt wait by the whole decode.
	const slots = readSource('src/lib/rb/deck-slots.ts');
	assert.ok(slots.includes('pressT0Ms?: number;'), 'the pending intent must carry the stamp');
	const panel = readSource('src/lib/components/rb/BrowserPanel.svelte');
	assert.ok(
		panel.includes('pendingPlay.pressT0Ms'),
		'the post-load play must spend the stamp the keydown stored'
	);
});

test('a controller press is stamped at MIDI receipt, like a DOM press', () => {
	// The primary hardware surface. webmidi took the receipt time all along
	// and then dropped it at the glue layer, so every physical play/cue filed
	// a plain schedule row.
	const midi = readSource('src/lib/rb/midi/webmidi.svelte.ts');
	assert.ok(
		midi.includes('_actionHandler(binding.action, value, device.id, log.ts)'),
		'the receipt stamp must reach the glue layer'
	);
	// Pinned on the registration signature, not on the whole file, and on
	// either optionality: main tightened the slot to a required `number`
	// (d0b0840b) after this guard was written against the optional form.
	const registerAt = midi.indexOf('export function registerActionHandler(');
	assert.ok(registerAt >= 0, 'registerActionHandler must exist for this guard to pin anything');
	const registerSig = midi.slice(registerAt, midi.indexOf('): void {', registerAt));
	assert.ok(
		/\bpressT0Ms\??: number\b/.test(registerSig),
		'registerActionHandler must accept the receipt stamp as a fourth parameter'
	);
	const glue = readSource('src/lib/rb/midi/action-glue.svelte.ts');
	assert.ok(
		glue.includes('_cmdPlayToggle(action.deck, pressT0Ms)'),
		'a physical play must spend it'
	);
	assert.ok(glue.includes('_cmdPressCue(action.deck, pressT0Ms)'), 'and so must a physical cue');
});

//-----------------------------------------------------------------------------
// P1 BLOCKING r3974057968: mark the load-spanning row before aggregation
//-----------------------------------------------------------------------------

test('the deferred play marks its stamp as load-spanning before spending it', () => {
	// This dispatch fires only after `load` above it has been awaited, so a
	// stamp reaching it always spans this deck's load - mark it, or an S2
	// consumer has no way to exclude it short of guessing from duration.
	const panel = readSource('src/lib/components/rb/BrowserPanel.svelte');
	const fn = panel.slice(
		panel.indexOf('async function _loadOntoDeck('),
		panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
	);
	const markAt = fn.indexOf('markLoadSpanningPress(pendingPlay.pressT0Ms)');
	// c8d78c993 (LATENCY-02 QUANTIZED LAUNCH) made the deferred play literal multi-line
	// to carry pendingPlay.quantize; ordering is pinned against that new shape.
	const dispatchAt = fn.search(/\{\s*type: 'play',\s*deck: target,\s*playing: true,/);
	assert.ok(markAt !== -1, 'the deferred play must mark its stamp load-spanning');
	assert.ok(dispatchAt !== -1, 'the deferred play dispatch must exist to order the mark against');
	assert.ok(
		markAt < dispatchAt,
		'marking after the dispatch races scheduleRowFacts reading the classification'
	);
	// And the marker is imported from the module that owns the registry.
	assert.ok(
		panel.includes('markLoadSpanningPress') &&
			/import\s*\{[^}]*markLoadSpanningPress[^}]*\}\s*from\s*'\$lib\/rb\/deck-slots'/.test(panel),
		'the marker must come from deck-slots, not be reinvented locally'
	);
});

//-----------------------------------------------------------------------------
// P1 BLOCKING PRRT_kwDOSEvNd86g96Le: mark the armed hot-cue row before arming
//-----------------------------------------------------------------------------

test('the armed hot-cue dispatch marks its stamp as armed before arming the trigger', () => {
	// planHotCueTrigger's 'armed' plan defers to the next downbeat; the mark
	// must land before _hotCueDriver.arm, the same ordering requirement as
	// the load-spanning marker above, or a schedule that resolves inside the
	// same microtask could read the classification before it is set.
	const ipc = readSource('src/lib/rb/performance-ipc.svelte.ts');
	const fn = ipc.slice(
		ipc.indexOf("command.type === 'hot_cue_trigger'"),
		ipc.indexOf("command.type === 'auto_play_two_track'")
	);
	const markAt = fn.indexOf('markArmedHotCuePress(pressT0Ms)');
	const armAt = fn.indexOf('await _hotCueDriver.arm(');
	assert.ok(markAt !== -1, 'the armed branch must mark its stamp');
	assert.ok(armAt !== -1, 'the armed branch must still call _hotCueDriver.arm');
	assert.ok(markAt < armAt, 'marking after arm races scheduleRowFacts reading the classification');
	// The immediate branch must NOT mark - an immediate jump is not deferred,
	// so marking it would misclassify an ordinary Class A press as armed.
	const immediateAt = fn.indexOf("plan.kind === 'immediate'");
	const immediateBranch = fn.slice(immediateAt, markAt === -1 ? fn.length : markAt);
	assert.ok(
		!immediateBranch.includes('markArmedHotCuePress'),
		'an immediate hot-cue jump must never be marked armed'
	);
	// And the marker is imported from the module that owns the registry.
	assert.ok(
		/import\s*\{[^}]*markArmedHotCuePress[^}]*\}\s*from\s*'\$lib\/rb\/deck-slots'/.test(ipc),
		'the marker must come from deck-slots, not be reinvented locally'
	);
});

//-----------------------------------------------------------------------------
// P2 BLOCKING r3974057968: capture the initiating load-and-play press
//-----------------------------------------------------------------------------

test('a direct Load+Play (TrackTable double-click) threads its own click stamp', () => {
	// The preexisting opts.play === true entry path distinct from Space: a
	// TrackTable row double-click never supplied a timestamp at all, so
	// pendingPlay.pressT0Ms stayed undefined and the eventual play filed as
	// an unmeasured plain schedule.
	const table = readSource('src/lib/components/rb/browser/TrackTable.svelte');
	const dblclick = table.slice(
		table.indexOf('function onRowDblClick('),
		table.indexOf('function hl(')
	);
	assert.ok(
		dblclick.includes('pressT0Ms: event.timeStamp'),
		'the no-confirm fast path must forward the double-click\'s own stamp'
	);
	// The confirm-before-load dialog pauses for a human decision of unknown
	// length; the felt wait for this gesture starts at the Yes click actually
	// requesting play, not at the earlier double-click that only opened it.
	const yesButtonAt = table.indexOf('class="load-confirm-yes"');
	const yesButtonBody = table.slice(yesButtonAt, table.indexOf('>Yes</button', yesButtonAt));
	assert.ok(
		yesButtonBody.includes('onclick={(e) =>'),
		'the Yes button must take its own click event to stamp from'
	);
	assert.ok(
		yesButtonBody.includes('pressT0Ms: e.timeStamp'),
		'and forward that stamp into onloadrow, or the parameter is decorative'
	);

	const panel = readSource('src/lib/components/rb/BrowserPanel.svelte');
	const fn = panel.slice(
		panel.indexOf('async function _loadOntoDeck('),
		panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
	);
	assert.ok(
		/dispatchPerformanceCommand\(\s*\{\s*type: 'load_play_intent',[\s\S]*?\},\s*opts\.pressT0Ms\s*\);/.test(fn),
		'_loadOntoDeck must forward the caller-supplied stamp into load_play_intent, ' +
			'the same command setPendingLoadPlayIntent already reads a stamp from'
	);
});

test('the suggestion-panel Load+Play chevrons thread their own click stamp', () => {
	// A second, distinct opts.play === true entry path beside TrackTable's:
	// SuggestNextStrip's chevron and RecommendedSection's paired-row
	// double-click called loadSuggest(sid, { play: true }) with no timestamp
	// at all, so this pair of gestures filed as unmeasured plain schedules
	// exactly like the TrackTable gap above, before either was stamped.
	const strip = readSource('src/lib/components/rb/SuggestNextStrip.svelte');
	assert.ok(
		strip.includes('onplay?: (stableId: string, pressT0Ms: number) => void;'),
		'the strip must declare its onplay callback wide enough to carry a stamp'
	);
	assert.ok(
		strip.includes('onclick={(e) => onplay?.(cand.stable_id, e.timeStamp)}'),
		'and the play button must hand on its own click event stamp'
	);

	const recommended = readSource('src/lib/components/rb/RecommendedSection.svelte');
	assert.ok(
		recommended.includes('onplay?: (stableId: string, pressT0Ms: number) => void;'),
		'RecommendedSection must declare the same widened onplay callback'
	);
	assert.ok(
		recommended.includes("ondblclick={(e) => onplay?.(p.partnerId, e.timeStamp)}"),
		'and the paired-row double-click must hand on its own event stamp'
	);

	const panel = readSource('src/lib/components/rb/BrowserPanel.svelte');
	assert.ok(
		panel.includes(
			'function loadSuggest(sid: string, opts: { play?: boolean; pressT0Ms?: number } = {}): void {'
		),
		'loadSuggest must accept a stamp rather than dropping it on the floor'
	);
	assert.ok(
		/loadRow\(row, picked\.deck, \{\s*play: true,\s*reservation: picked\.reservation,\s*\.\.\.\(opts\.pressT0Ms === undefined \? \{\} : \{ pressT0Ms: opts\.pressT0Ms \}\)\s*\}\);/.test(
			panel
		),
		'and forward it into loadRow, or the parameter is decorative'
	);
	const onplayCallSites = panel.match(
		/onplay=\{\(sid, pressT0Ms\) => loadSuggest\(sid, \{ play: true, pressT0Ms \}\)\}/g
	);
	assert.equal(
		onplayCallSites?.length,
		2,
		'both SuggestNextStrip and RecommendedSection must be wired with the stamp, not just one'
	);
});

test('QuickDrawMenu Play/Pause and its contextual actions thread their own click stamp', () => {
	// The gap the "every P0 press path" test above does not reach: QuickDrawMenu
	// is a THIRD dispatcher onto runPerformanceCommandFromUi, separate from
	// TransportCluster/Deck/hotkeys, and its click handlers discarded the
	// click event entirely, so _togglePlay/_runAction fell back to
	// runPerformanceCommandFromUi's own performance.now() default - a stamp
	// taken downstream of the browser input queue and of handler dispatch,
	// silently understating the wait under any main-thread backlog.
	const menu = readSource('src/lib/components/rb/QuickDrawMenu.svelte');
	assert.ok(
		menu.includes(
			'async function _togglePlay(deck: DeckId, pressT0Ms?: number): Promise<void> {'
		),
		'_togglePlay must accept a stamp rather than dropping it on the floor'
	);
	assert.ok(
		menu.includes(
			"await runPerformanceCommandFromUi({ type: 'play', deck, playing: !playing }, pressT0Ms);"
		),
		'and forward it into the play command, or the parameter is decorative'
	);
	assert.ok(
		menu.includes('onclick={(e) => void _togglePlay(deck, e.timeStamp)}'),
		'the Play/Pause quick-draw button must hand on its own click event stamp'
	);

	assert.ok(
		menu.includes(
			'async function _runAction(id: QuickDrawActionId, deck: DeckId, pressT0Ms?: number): Promise<void> {'
		),
		'_runAction must accept a stamp rather than dropping it on the floor'
	);
	assert.ok(
		menu.includes('await runPerformanceCommandFromUi(quickDrawCommand(id, deck), pressT0Ms);'),
		'and forward it into the built command, or the parameter is decorative'
	);
	assert.ok(
		menu.includes("onclick={(e) => void _runAction('unload', deck, e.timeStamp)}"),
		'the tall Unload button must hand on its own click event stamp'
	);
	assert.ok(
		menu.includes('onclick={(e) => void _runAction(leaf, deck, e.timeStamp)}'),
		'the loop-leaf buttons must hand on their own click event stamp'
	);

	assert.ok(
		menu.includes('run: (pressT0Ms?: number) => Promise<void>;'),
		'the generic contextual-action item must widen its run() to carry a stamp'
	);
	assert.ok(
		menu.includes('void item.run(e.timeStamp);'),
		'and the generic contextual-action button must hand on its own click event stamp'
	);
});
