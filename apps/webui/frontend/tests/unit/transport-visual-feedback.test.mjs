// requirement: LATENCY-01
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	engineBlockAfter,
	SCHEDULE_DECK_ANCHOR,
	SCHEDULE_DECK_SERIAL_ANCHOR
} from './engine-source.mjs';

/**
 * LATENCY-01 visual feedback: the control reflects the input on the NEXT frame
 * (<=16ms at 60Hz), independently of what the audio thread is doing.
 *
 * Two structural problems were measured on the live app, both of which passed
 * a stopwatch while being wrong by construction:
 *
 * 1. `st.playing` - the value the play glyph, aria-pressed and data-state all
 *    read - was written only AFTER `await processor.schedule(...)`, i.e. after
 *    an AudioWorklet MessagePort round trip. The button measured fine (p50
 *    3.4ms) purely because that RPC is currently ~2ms. It was not independently
 *    fast; a worklet stall, a stem deck's four parallel schedules, or a busy
 *    sync scope would drag the glyph along with the audio.
 * 2. The transport buttons were disabled by the `pending` flag. The observed
 *    DOM mutation sequence for ONE click was disabled@1.8ms ->
 *    data-state@2.8ms -> disabled@6.2ms: greyed out about one frame before it
 *    flipped, then re-enabled. On a 60Hz display that is a visible blink on
 *    every press, and a control that flickers reads as slow whatever the clock
 *    says.
 *
 * Neither is a value any module returns to a caller - one is an assignment
 * ordering, the other is a Svelte attribute binding - so both are pinned as
 * source text.
 *
 * Regression lines:
 * - if st.playing is assigned only after the worklet ack then visual feedback
 *   is causally downstream of the audio thread and stalls with it
 * - if a failed schedule does not reconcile st.playing then a deck that never
 *   started keeps showing the pause glyph
 * - if `pending` reaches `disabled` on a Class A control again then every press
 *   blinks the button one frame before it flips
 * - if the in-flight state stops being observable at all then agents and
 *   assistive tech lose the signal the disable used to carry
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

/** Component source, positively located: a guard that cannot find its file must
 * fail rather than assert against ''. */
function componentSource(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(
		text.trim().length > 0,
		`if ${relativePath} reads empty then this guard silently asserts nothing`
	);
	return text;
}

//-----------------------------------------------------------------------------
// the glyph must not wait on the audio thread
//-----------------------------------------------------------------------------

test('play state is written as intent, in the same turn as the input', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_ANCHOR);
	const optimisticAt = body.indexOf('deckStates[deck].playing =');
	const firstAwaitAt = body.indexOf('await ');

	assert.notEqual(
		optimisticAt,
		-1,
		'if the intent is never written before the schedule then the play glyph waits on ' +
			'the AudioWorklet round trip and stalls whenever it does'
	);
	assert.notEqual(firstAwaitAt, -1, 'the schedule path must still be asynchronous');
	assert.ok(
		optimisticAt < firstAwaitAt,
		'the optimistic write must land BEFORE the first await, i.e. in the synchronous ' +
			'turn the input handler is still on; after any await it is another frame away'
	);
});

test('the intent write sits alongside the transport intent it mirrors', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_ANCHOR);
	const desiredAt = body.indexOf('rt.desiredActive = active;');
	const playingAt = body.indexOf('deckStates[deck].playing =');
	assert.notEqual(desiredAt, -1);
	assert.ok(
		desiredAt < playingAt,
		'st.playing must mirror the standing transport intent, so it is written after it'
	);
});

test('a rejected schedule reconciles the optimistic write to the standing intent', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_ANCHOR);
	assert.ok(
		body.includes('} catch (error) {'),
		'without a catch, an optimistic play that fails to schedule leaves the deck showing ' +
			'the pause glyph forever'
	);
	assert.ok(
		body.includes('rt.desiredActive'),
		'the reconcile must go to the STANDING intent, not to a captured previous value: a ' +
			'newer command may already have superseded this one, and restoring a stale value ' +
			'would clobber it'
	);
	assert.ok(
		body.includes('throw error;'),
		'reconciling must not swallow the failure - the caller still has to see it'
	);
});

test('the post-ack write stays as the reconcile-to-truth', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_SERIAL_ANCHOR);
	assert.ok(
		body.includes('rt.desiredActive'),
		'the optimistic write is a prediction; the post-ack write is what makes it true, and ' +
			'removing it would leave a superseded schedule showing the wrong glyph'
	);
});

//-----------------------------------------------------------------------------
// the button must not blink
//-----------------------------------------------------------------------------

test('a pending command never greys out the play or cue button', () => {
	const text = componentSource('lib/components/rb/deck/TransportCluster.svelte');
	assert.ok(
		!text.includes('disabled={!hasTrack || pending}'),
		'if `pending` reaches `disabled` again then the measured disabled@1.8ms -> ' +
			'data-state@2.8ms -> disabled@6.2ms blink is back on every press'
	);
	assert.equal(
		text.split('disabled={!hasTrack}').length - 1,
		2,
		'both transport buttons must stay disabled for the one honest reason - no track ' +
			'loaded, where the engine genuinely throws'
	);
});

test('a pending command never greys out a hot cue pad', () => {
	const text = componentSource('lib/components/rb/deck/HotCueBank.svelte');

	// Scoped to the PADS. The undo button below them carries its own guard,
	// asserted in hot-cue-rename-on-create.test.mjs: it is often the control
	// clicked to leave an open cue-name input, so a `pending` disable there eats
	// the click that started the write rather than merely flickering.
	const padAt = text.indexOf('class="slot"');
	assert.notEqual(padAt, -1, 'if the hot cue pad markup moved then this guard is pointed at nothing');
	const padTagEnd = text.indexOf('>', padAt);
	assert.notEqual(padTagEnd, -1, 'unterminated hot cue pad tag');
	const padTag = text.slice(padAt, padTagEnd);

	assert.ok(
		!padTag.includes('pending ||') && !padTag.includes('|| pending'),
		'a transport command in flight must not grey out all eight pads'
	);
	assert.ok(
		padTag.includes('disabled={busySlot === entry.slot || renameSlot === entry.slot}'),
		'the per-slot write guard must only disable the slot being written (or mid-rename, pin ' +
			'c20eeb07cae0): a blur-triggering click has to remain actionable, then wait for the ' +
			'current write before it starts its own'
	);
	assert.ok(
		padTag.includes('aria-busy={pending}'),
		'removing the disable must not also remove the signal it carried'
	);
});

test('in-flight state stays observable without touching layout or interactivity', () => {
	for (const relativePath of [
		'lib/components/rb/deck/TransportCluster.svelte',
		'lib/components/rb/deck/HotCueBank.svelte'
	]) {
		const text = componentSource(relativePath);
		assert.ok(
			text.includes('aria-busy={pending}'),
			`if ${relativePath} drops aria-busy then removing the disable also removed the ` +
				'signal agents and assistive tech used to read, which is a different bug'
		);
	}
});

//-----------------------------------------------------------------------------
// the latency term the instrument reports must not go quietly stale
//-----------------------------------------------------------------------------

test('the processor latency snapshot is re-read live and drift is loud', () => {
	const body = engineBlockAfter(`async function _observeLiveProcessorLatency(
	deck: DeckId,
	processor: _DeckProcessor
): Promise<void> {`);
	assert.ok(
		body.includes('await processor.latencySec()'),
		'rt.latencySec is read once at load, but Signalsmith re-reads both latency terms on ' +
			'every configure(); without a live re-read the schedule floor and the logged ' +
			'processor_latency_ms both quote a dead number after any reconfiguration'
	);
	assert.ok(
		body.includes("recordPerfEvent(\n\t\t'processor-latency-drift'"),
		'a moved latency must be recorded, not absorbed: every schedule taken before it used ' +
			'the stale value and the logged offsets are wrong by that difference'
	);
	assert.ok(
		body.includes('rt.latencySec = liveLatencySec;'),
		'the snapshot must heal, or the drift event fires forever and nothing improves'
	);
	assert.ok(
		body.includes('if (rt.processor !== processor) return;'),
		'a reading from a processor the deck has already replaced must not be written back'
	);
});

// REQ: LATENCY-02
test('LATENCY-02 TransportCluster keeps pending off disabled and shows armed countdown', () => {
	const source = componentSource('lib/components/rb/deck/TransportCluster.svelte');
	assert.doesNotMatch(source, /disabled=\{pending\}/);
	assert.match(source, /data-state=\{armed \? 'armed'/);
	assert.match(source, /class:armed/);
	assert.match(source, /QUANTIZED LAUNCH/);
	assert.match(source, /countdown/);
});

test('the live latency re-read never sits on the transport path', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_SERIAL_ANCHOR);
	assert.ok(
		body.includes('void _observeLiveProcessorLatency(deck, processor);'),
		'the re-read must be fire-and-forget; awaiting a MessagePort round trip here would ' +
			'add latency to the very path this work shortens'
	);
	assert.ok(
		!body.includes('await _observeLiveProcessorLatency('),
		'if the re-read is awaited then every transport command pays for a diagnostic'
	);
	const scheduleAt = body.indexOf('await processor.schedule(');
	const observeAt = body.indexOf('void _observeLiveProcessorLatency(');
	assert.ok(
		scheduleAt < observeAt,
		'the re-read must happen after the schedule is posted, so it cannot delay it'
	);
});
