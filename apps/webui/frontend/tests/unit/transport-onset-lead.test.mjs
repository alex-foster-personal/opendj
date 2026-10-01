import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { engineBlockAfter, SCHEDULE_DECK_SERIAL_ANCHOR } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LATENCY round 2, step 1: lead by the measured ONSET RAMP, not by the
 * processor's self-report.
 *
 * Round 1 cut the scheduled offset from 220ms to 128ms by splitting one safety
 * constant in two. The remaining 128ms was 8ms of immediate safety plus the
 * whole 120ms the Signalsmith worklet reports from `latency()`. Round 2's
 * design spike established, by measurement, that the 120ms is the WRONG
 * QUANTITY rather than a conservative one:
 *
 * - `latency()` is a LIVE-INPUT figure. Our decks always play from loaded
 *   buffers, on a worklet path that re-seeks every render quantum and
 *   compensates the term itself. Given ample lead, audio arrives EXACTLY when
 *   scheduled: 250ms scheduled -> 250.3ms onset. None of the 120ms shows up as
 *   playback delay, so leading by it bought nothing.
 * - What a short lead really costs is a SOFT START. Lateness to 90 percent of
 *   steady RMS is `max(0, RAMP - lead)` with `RAMP ~= 0.37 x blockMs`, and
 *   `latency()` equals blockMs exactly, so the ramp is derivable from the
 *   number every deck already caches.
 *
 * The implementation-time verification then corrected the constant from that
 * 0.37 mean fit to 0.39. The lead must COVER the ramp, so the right value is
 * the largest knee observed rather than the average: 0.37 x 120 = 44.4ms sits
 * just under the shipped block's 45.6ms knee and measured 4.53ms late, while
 * 0.39 x 120 = 46.8ms measured -0.04ms, i.e. indistinguishable from a 128ms
 * lead. Every block size swept is at or inside its own noise floor at 0.39.
 *
 * So the transport lead becomes 0.39 x the self-report: 46.8ms instead of
 * 120ms at the shipped block, for a 54.8ms scheduled offset instead of 128ms,
 * with measurably identical onset behaviour and byte-identical audio.
 *
 * Regression lines:
 * - if the plain transport lead is the processor's self-report again then the
 *   scheduled offset is back at 128ms and ~73ms is being paid for nothing
 * - if the lead stops covering the measured knee then the offset improves
 *   while the start goes audibly soft, which is a worse trade wearing the
 *   costume of a better number
 * - if the derived lead is not strictly smaller than the self-report then the
 *   derivation was inverted and the fix is a regression
 * - if the ramp factor leaves (0, 1] then it is no longer a fraction of the
 *   block and the lead is not an onset ramp at all
 * - if any plain-transport call site stops routing through _transportLeadSec
 *   then that control silently keeps the old floor
 * - if beat sync stops planning from the raw self-report then the group launch
 *   instant can fall inside a participant's onset ramp and the first beat smears
 * - if block configuration stops being global then two decks have different
 *   ramps and a synced launch smears
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

/** The shipped Signalsmith default block at 44100Hz, as `latency()` reports it. */
const SHIPPED_BLOCK_LATENCY_S = 0.12;
/** What round 1 left on the click path, and what this round must beat. */
const ROUND_1_OFFSET_MS = 128;
/**
 * ABSOLUTE ceiling for the plain-transport offset at the shipped block. Paired
 * deliberately with the relative check below: round 1's lesson was that a
 * purely relative guard ("smaller than before") passes for a change that moved
 * the number by one millisecond. The e2e floor ratchets to this same 60ms.
 */
const STEP_1_OFFSET_CEILING_MS = 60;
/**
 * ABSOLUTE floor. The lead must still COVER the ramp, or the start is soft:
 * cutting the offset to 8ms does not give an 8ms response, it gives 8 + 35 =
 * 43ms of lateness. A guard with only a ceiling would call that an improvement.
 */
const STEP_1_OFFSET_FLOOR_MS = 40;
/**
 * Resolution of the ramp sweep itself. The apparatus reads onset against a
 * 64-sample sliding RMS, and its own noise floor is ~1.5ms: a long lead
 * measures 0.96ms late at block 120 and 1.25ms at block 20 when the true answer
 * is zero. 0.5ms is well inside that, and exists for exactly one row - block
 * 20's ramp reads 7.9ms while 0.39x gives 7.8ms, a 0.1ms shortfall an order of
 * magnitude below what the sweep can resolve. The KNEE bound below is the sharp
 * check; this one must not fail on a rounding artefact.
 */
const RAMP_SWEEP_RESOLUTION_MS = 0.5;

let constants;
let math;
let adapter;

before(async () => {
	constants = await loadTypeScriptModule('src/lib/player/constants.ts');
	math = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
	adapter = await loadTypeScriptModule('src/lib/rb/stretch-adapter.ts');
});

/** Source text of one frontend module, positively located. */
function moduleSource(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(
		text.trim().length > 0,
		`if ${relativePath} reads empty then this guard silently asserts nothing`
	);
	return text;
}

//-----------------------------------------------------------------------------
// the lead arithmetic
//-----------------------------------------------------------------------------

test('the onset-ramp factor is the measured fraction of the block, not a round number', () => {
	assert.equal(constants.PROCESSOR_ONSET_RAMP_FACTOR, 0.39);
	assert.ok(
		constants.PROCESSOR_ONSET_RAMP_FACTOR > 0 && constants.PROCESSOR_ONSET_RAMP_FACTOR <= 1,
		'the ramp is a FRACTION of the block length; outside (0, 1] it is not a ramp'
	);
});

test('the derived lead COVERS the measured ramp at every block size swept', () => {
	// blockMs -> measured lateness to 90 percent of steady RMS at lead 0, i.e.
	// the ramp itself. The lead must sit at or above each, because a lead below
	// the ramp is spent as audible softness rather than saved. This is the
	// correction the implementation-time knee sweep bought: fitting the MEAN of
	// these (0.37) left the shipped 120ms block 4.5ms soft, since 0.37 x 120 =
	// 44.4ms lands under its 45.6ms knee.
	for (const [blockMs, measuredRampMs] of [
		[120, 43.4],
		[60, 21.9],
		[40, 14.9],
		[30, 11.4],
		[20, 7.9],
		[10, 3.8]
	]) {
		const leadMs = math.processorOnsetLeadSec(blockMs / 1000) * 1000;
		assert.ok(
			leadMs >= measuredRampMs - RAMP_SWEEP_RESOLUTION_MS,
			`the derived lead at block ${blockMs}ms is ${leadMs.toFixed(2)}ms, below the ` +
				`measured ${measuredRampMs}ms ramp: that shortfall is paid as a soft start, ` +
				'and the scheduled offset improves while what the DJ hears gets worse'
		);
		assert.ok(
			leadMs - measuredRampMs <= 4,
			`the derived lead at block ${blockMs}ms clears the ramp by ` +
				`${(leadMs - measuredRampMs).toFixed(2)}ms; beyond the knee the lead is dead ` +
				'weight again, which is the whole defect round 2 set out to remove'
		);
	}
});

test('the factor clears the measured KNEE at each block, which the mean fit did not', () => {
	// Measured directly, reproducible bit-for-bit across runs. Below its knee a
	// block is audibly soft even though the ramp fit says it should be clean.
	for (const [blockMs, kneeMs] of [
		[120, 45.6],
		[60, 22.2],
		[30, 11.7]
	]) {
		const leadMs = math.processorOnsetLeadSec(blockMs / 1000) * 1000;
		assert.ok(
			leadMs >= kneeMs,
			`block ${blockMs}ms has its onset knee at ${kneeMs}ms and the lead is ` +
				`${leadMs.toFixed(2)}ms; 0.37 x 120 = 44.4ms measured 4.53ms late here, which ` +
				'is exactly the regression this bound exists to stop coming back'
		);
	}
});

test('the derived lead is strictly smaller than the self-report it replaced', () => {
	for (const latencySec of [0.01, 0.02, 0.03, 0.06, SHIPPED_BLOCK_LATENCY_S]) {
		const lead = math.processorOnsetLeadSec(latencySec);
		assert.ok(
			lead < latencySec,
			`if the lead at latency ${latencySec}s is ${lead}s then round 2 made the schedule ` +
				'slower, not faster'
		);
		assert.ok(lead > 0, 'a zero lead means every start pays the whole ramp as lateness');
	}
	// A zero-latency processor asks for no lead: the safety margin is the whole
	// floor, and safeTransportScheduleTime still refuses to schedule at zero.
	assert.equal(math.processorOnsetLeadSec(0), 0);
});

test('a plain transport schedule at the shipped block lands inside the step-1 window', () => {
	const now = 10;
	const lead = math.processorOnsetLeadSec(SHIPPED_BLOCK_LATENCY_S);
	const offsetMs = (math.safeTransportScheduleTime(now, lead) - now) * 1000;

	// RELATIVE: it must actually be better than what round 1 shipped.
	assert.ok(
		offsetMs < ROUND_1_OFFSET_MS,
		`if the offset is ${offsetMs}ms then it did not improve on round 1's ${ROUND_1_OFFSET_MS}ms`
	);
	// ABSOLUTE, both sides. A relative-only guard passes a one-millisecond
	// change; a ceiling-only guard passes a lead that no longer covers the ramp.
	assert.ok(
		offsetMs <= STEP_1_OFFSET_CEILING_MS,
		`scheduled offset ${offsetMs}ms exceeds the step-1 ceiling ${STEP_1_OFFSET_CEILING_MS}ms`
	);
	assert.ok(
		offsetMs >= STEP_1_OFFSET_FLOOR_MS,
		`scheduled offset ${offsetMs}ms is below the ${STEP_1_OFFSET_FLOOR_MS}ms floor: the ` +
			'lead no longer covers the 43.4ms onset ramp, so starts are audibly soft even ' +
			'though the logged number looks better'
	);
	assert.ok(
		Math.abs(offsetMs - 54.8) < 1e-9,
		`the four-term decomposition says 46.8ms ramp lead + 8ms safety = 54.8ms, got ${offsetMs}ms`
	);
});

test('the step-2 block would land the offset near 20ms, and still cover its own ramp', () => {
	// Not shipped (STRETCH_BLOCK_MS is null); pinned so the number quoted in the
	// design table cannot drift away from the arithmetic that produced it.
	const now = 10;
	const lead = math.processorOnsetLeadSec(0.03);
	const offsetMs = (math.safeTransportScheduleTime(now, lead) - now) * 1000;
	assert.ok(
		Math.abs(offsetMs - 19.7) < 1e-9,
		`block 30ms: 11.7ms ramp lead + 8ms safety = 19.7ms, got ${offsetMs}ms`
	);
	assert.ok(
		Math.abs(offsetMs - lead * 1000 - constants.TRANSPORT_IMMEDIATE_SAFETY_S * 1000) < 1e-9,
		'the immediate safety margin must survive the block change intact, not be absorbed ' +
			'into a smaller-looking headline offset'
	);
});

//-----------------------------------------------------------------------------
// sabotage: the guards must refuse an inverted or absent derivation
//-----------------------------------------------------------------------------

test('SABOTAGE: a ramp factor outside (0, 1] is refused', () => {
	assert.throws(() => math.processorOnsetLeadSec(0.12, 1.0001), /exceeds 1/);
	assert.throws(() => math.processorOnsetLeadSec(0.12, 0), /greater than zero/);
	assert.throws(() => math.processorOnsetLeadSec(0.12, -0.39), /rampFactor/);
	assert.throws(() => math.processorOnsetLeadSec(-0.12), /processorLatencySec/);
	assert.throws(() => math.processorOnsetLeadSec(Number.NaN), /processorLatencySec/);
	// The boundary is legal: a processor whose ramp IS its whole reported term.
	assert.equal(math.processorOnsetLeadSec(0.12, 1), 0.12);
});

test('SABOTAGE: a factor of 1 puts the offset back where round 1 left it', () => {
	// The exact regression this file exists to catch, stated as arithmetic: if
	// anyone "simplifies" the factor away, the offset returns to 128ms and the
	// absolute ceiling above is what fails.
	const now = 10;
	const offsetMs =
		(math.safeTransportScheduleTime(now, math.processorOnsetLeadSec(SHIPPED_BLOCK_LATENCY_S, 1)) -
			now) *
		1000;
	assert.ok(
		Math.abs(offsetMs - ROUND_1_OFFSET_MS) < 1e-9,
		`a factor of 1 must reproduce round 1 exactly, got ${offsetMs}ms`
	);
	assert.ok(
		offsetMs > STEP_1_OFFSET_CEILING_MS,
		'if the round-1 offset still passed the step-1 ceiling then the ceiling is decorative'
	);
});

//-----------------------------------------------------------------------------
// beat sync keeps the conservative term, and keeps clearing the ramp
//-----------------------------------------------------------------------------

test('beat sync still plans from the self-report, not the shortened lead', () => {
	const now = 10;
	const syncAt = math.safeSyncScheduleTime(now, SHIPPED_BLOCK_LATENCY_S, now);
	assert.ok(
		Math.abs(syncAt - (now + SHIPPED_BLOCK_LATENCY_S + constants.SYNC_SCHEDULE_SAFETY_S)) < 1e-12,
		'if a group launch shortened with step 1 then the two changes are entangled and a ' +
			'sync regression would be blamed on the transport fix'
	);
	assert.ok(
		syncAt > math.safeTransportScheduleTime(now, math.processorOnsetLeadSec(SHIPPED_BLOCK_LATENCY_S)),
		'the sync launch must stay strictly further out than a plain transport schedule'
	);
});

test('a group launch clears the worst participant onset ramp with margin', () => {
	// The NEW constraint round 2 introduces. A shared instant that falls inside
	// a participant's ramp does not make it late, it makes it SOFT relative to
	// the others, which is a smeared first beat rather than a missed one.
	const now = 10;
	for (const blockSec of [0.01, 0.02, 0.03, 0.06, SHIPPED_BLOCK_LATENCY_S]) {
		const groupLeadSec = math.safeSyncScheduleTime(now, blockSec, now) - now;
		const rampSec = math.processorOnsetLeadSec(blockSec);
		assert.ok(
			groupLeadSec > rampSec * 2,
			`at block ${blockSec}s the group lead ${groupLeadSec}s must clear the ${rampSec}s ` +
				'ramp with room, or the first beat of a synced launch smears'
		);
	}
});

//-----------------------------------------------------------------------------
// wiring: every plain-transport floor goes through the one helper
//-----------------------------------------------------------------------------

test('the engine derives the transport lead in exactly one place', () => {
	const body = engineBlockAfter('function _transportLeadSec(deck: DeckId): number {');
	assert.ok(
		body.includes('processorOnsetLeadSec('),
		'if the helper stops deriving the lead then whatever it returns instead is what ' +
			'every Class A control schedules against'
	);
	assert.ok(
		body.includes('latencySec'),
		'the derivation reads the cached self-report; if it stops, it is reading something else'
	);
});

test('no plain-transport call site passes a raw self-report as the lead', () => {
	const text = moduleSource('lib/rb/audio-engine.svelte.ts');
	const calls = text.split('safeTransportScheduleTime(').slice(1);
	assert.ok(
		calls.length >= 6,
		`if fewer than six plain-transport floors remain (found ${calls.length}) then a ` +
			'control stopped routing through the shared helper and has its own margin'
	);
	for (const call of calls) {
		const args = call.slice(0, call.indexOf(')') + 1);
		assert.ok(
			args.includes('_transportLeadSec(') || args.includes('processorLeadSec'),
			`a plain-transport floor is computed from "${args.trim()}" rather than the ramp ` +
				'lead; passing latencySec here restores the 128ms schedule'
		);
		assert.ok(
			!args.includes('.latencySec'),
			`"${args.trim()}" hands the processor self-report straight to the floor, which is ` +
				'exactly the 78ms of dead weight round 2 removed'
		);
	}
});

test('the instrument is fed BOTH the lead charged and the self-report', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_SERIAL_ANCHOR);
	assert.ok(
		body.includes('const processorLeadSec = _transportLeadSec(deck);'),
		'the lead must be a const the floor and the log both read, or they can disagree ' +
			'about what was charged'
	);
	assert.ok(
		body.includes('safeTransportScheduleTime(scheduleContextTime, processorLeadSec)'),
		'if the floor is computed from anything but that const then the logged safety_ms ' +
			'describes a schedule that did not happen'
	);
	assert.ok(
		body.includes('processorLatencySec: rt.latencySec'),
		'the self-report must still be logged, or the gap round 2 opened is invisible'
	);
});

test('beat-sync scheduling still reads the raw latency, deliberately', () => {
	const body = engineBlockAfter(
		'async function _synchronizeFollowers(\n' +
			'\tmaster: DeckId,\n' +
			'\tfollowers: readonly DeckId[],\n' +
			'\toptions: _SyncOptions = {}\n' +
			'): Promise<void> {'
	);
	assert.ok(
		body.includes('const maxLatency = Math.max('),
		'if the group launch stops taking the max over participants then decks with ' +
			'different processors no longer share a floor'
	);
	assert.ok(
		!body.includes('_transportLeadSec('),
		'step 1 is explicitly scoped OUT of the sync path; shortening a group launch is a ' +
			'separate, separately gated change'
	);
});

//-----------------------------------------------------------------------------
// uniformity: one block for the whole fleet, or synced starts smear
//-----------------------------------------------------------------------------

test('a deck whose processor block differs from a loaded deck fails its load', () => {
	const body = engineBlockAfter(`function _assertUniformProcessorBlock(
	deck: DeckId,
	latencySec: number,
	sampleRateHz: number
): void {`);
	assert.ok(
		body.includes('1 / sampleRateHz'),
		'tolerance must be one sample: latency() quantises the block to whole samples, so ' +
			'exact equality would be brittle across sample rates'
	);
	assert.ok(
		body.includes('otherRuntime.processor === null'),
		'an unloaded deck has no block to disagree with and must not veto a load'
	);
	assert.ok(
		body.includes('throw new Error('),
		'a mixed fleet must fail the load that creates it, not go audibly wrong later'
	);

	const text = moduleSource('lib/rb/audio-engine.svelte.ts');
	assert.ok(
		text.includes('_assertUniformProcessorBlock(deck, latencySec, ctx.sampleRate);'),
		'if the check is never called from the load path then it protects nothing'
	);
	const assertAt = text.indexOf('_assertUniformProcessorBlock(deck, latencySec, ctx.sampleRate);');
	// #2155 (Sat 12 Sep 2026, commit 1db624ef7) added attachProcessor(), an
	// unrelated AudioEngine method with its own unguarded
	// `rt.latencySec = latencySec;` line earlier in the file. An unanchored
	// indexOf now finds THAT occurrence first, which is the exact "matches
	// more than once" ambiguity this test suite's own convention warns about -
	// search from assertAt so the guard pins the assignment this check
	// actually gates, not an unrelated same-text line.
	const publishAt = text.indexOf('rt.latencySec = latencySec;', assertAt);
	assert.ok(assertAt !== -1 && publishAt !== -1);
	assert.ok(
		assertAt < publishAt,
		'the check must run BEFORE the candidate processor is published, or a rejected ' +
			'deck is already in the fleet by the time it is rejected'
	);
});

test('stem branches must agree on the onset lead, not merely on reported latency', () => {
	const text = moduleSource('lib/rb/stem-graph.ts');
	assert.ok(
		text.includes('stem processor latency alignment mismatch'),
		'the original per-branch latency assertion must survive'
	);
	assert.ok(
		text.includes('processorOnsetLeadSec(latency)'),
		'reported-latency equality is not the property the transport floor consumes; the ' +
			'branches must agree on the LEAD, which is what round 2 schedules against'
	);
	assert.ok(
		text.includes('stem processor onset-lead mismatch'),
		'branches that reach full level at different times comb their own sum, and the ' +
			'failure must name that rather than read as a generic mismatch'
	);
});

//-----------------------------------------------------------------------------
// step 2: prepared, and provably off
//-----------------------------------------------------------------------------

test('the block-configuration flag ships OFF, so the audio is byte-identical', () => {
	assert.equal(
		adapter.STRETCH_BLOCK_MS,
		null,
		'step 2 is gated on the cross-lane quality methodology; flipping this default ' +
			'ships a measured frequency-resolution regression on a latency commit'
	);
	const text = moduleSource('lib/rb/stretch-adapter.ts');
	assert.ok(
		text.includes('if (blockMs !== null) {'),
		'a null flag must skip configure() entirely - calling it with the default preset ' +
			'is not the same code path the library takes when it is never called'
	);
	assert.ok(
		text.includes('node.configure({ blockMs })'),
		'if the configure call is absent then step 2 is not prepared, only described'
	);
});

test('the configured block must be the block the processor actually adopted', () => {
	// The hole a per-branch equality check cannot see: a configure() that
	// no-ops on EVERY branch leaves them agreeing at the wrong value.
	assert.doesNotThrow(() => adapter.assertStretchBlockApplied(0.03, 30));
	// latency() quantises to whole samples, so a fraction of a millisecond is fine.
	assert.doesNotThrow(() => adapter.assertStretchBlockApplied(0.005011, 5));
	assert.throws(
		() => adapter.assertStretchBlockApplied(0.12, 30),
		/did not take: asked for 30ms, processor reports 120ms/
	);
	assert.throws(() => adapter.assertStretchBlockApplied(0.03, 0), /blockMs/);
	assert.throws(() => adapter.assertStretchBlockApplied(-0.03, 30), /latency/i);
});

test('the configure call sits at creation, before any audio can exist', () => {
	const text = moduleSource('lib/rb/stretch-adapter.ts');
	const configureAt = text.indexOf('node.configure({ blockMs })');
	const loadAt = text.indexOf("async load(buffer: AudioBuffer, requested: PcmHandoff = 'copy'): Promise<PcmHandoff> {");
	assert.ok(configureAt !== -1 && loadAt !== -1);
	assert.ok(
		configureAt < loadAt,
		'configure() calls _reset() and reallocates the WASM scratch buffers; under a ' +
			'PLAYING deck that was measured to drop the output to effective silence for ' +
			'10-45ms, so it may only run while no audio is loaded'
	);
	assert.ok(
		text.includes('assertStretchBlockApplied(await processor.latencySec(), blockMs)'),
		'the adopted block must be read back from the processor, not assumed from the flag'
	);
});

//-----------------------------------------------------------------------------
// the AudioContext construction seam
//-----------------------------------------------------------------------------

test('AudioContext options travel through one named constant, and stay empty', () => {
	assert.deepEqual(
		constants.AUDIO_CONTEXT_OPTIONS,
		{},
		'no numeric latencyHint: 0.02 measured WORSE (48.0ms output against 32.0ms unset) ' +
			'because Chrome honours the hint upward too, and the only win (0.002) shrinks the ' +
			'output buffer and raises underrun risk under four decks of worklets'
	);
	assert.ok(
		Object.isFrozen(constants.AUDIO_CONTEXT_OPTIONS),
		'if the options object is mutable then any module can quietly change the device floor'
	);
	const build = engineBlockAfter('function _ensureGraph(): AudioContext {');
	assert.ok(
		build.includes('new AudioContext(AUDIO_CONTEXT_OPTIONS)'),
		'a bare new AudioContext() leaves a future user-facing buffer setting - which ' +
			'rekordbox, Serato and Traktor all ship - with nowhere to write to'
	);
});
