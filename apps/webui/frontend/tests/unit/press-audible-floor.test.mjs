import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter, readFrontendSource as readSource } from './engine-source.mjs';
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
	assert.equal(floor.scheduleRowKind(undefined), 'transport-schedule');
	assert.equal(floor.scheduleRowKind(0), 'transport-schedule-press');
	assert.equal(floor.scheduleRowKind(12.5), 'transport-schedule-press');
	// The suffix shape is what keeps every existing prefix-matching consumer
	// (the probe mirror, the e2e console grep) working unchanged.
	assert.ok(floor.PRESS_SCHEDULE_KIND.startsWith(floor.SCHEDULE_KIND));
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
	const body = engineBlockAfter(`async function _scheduleDeckSerial(
	deck: DeckId,
	when: number,
	inputSec: number | ((effectiveWhen: number) => number),
	active: boolean,
	tempoRatio: number | undefined,
	masterTempoEnabled: boolean | undefined,
	loop: LoopState | null | undefined,
	keyShiftSemitones: number | undefined,
	pressT0Ms: number | undefined
): Promise<number> {`);
	// The facts are DERIVED FROM scheduleStages, so the floor terms the labels
	// call absent are the same ones the row itself omits. A second read of the
	// context could disagree with the row printed beside it.
	assert.ok(
		body.includes('const row = scheduleRowFacts(scheduleStages, _ctx.state);'),
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
		cluster.includes('onclick={async (event) => await onPlayPause(event.timeStamp)}'),
		'the play button must hand on its own event stamp'
	);
	assert.ok(
		cluster.includes('onclick={async (event) => await onCue(event.timeStamp)}'),
		'and so must cue'
	);

	const deck = readSource('src/lib/components/rb/Deck.svelte');
	assert.ok(
		deck.includes('async function playPause(pressT0Ms?: number)') &&
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
		hotkeys.includes("{ type: 'play', deck, playing: !st.playing }, pressT0Ms"),
		'the keydown stamp must reach the command'
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
	const dispatchAt = body.indexOf("runPerformanceCommandFromUi({ type: 'play'");
	assert.ok(guardAt !== -1, 'the empty-deck guard must exist');
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
