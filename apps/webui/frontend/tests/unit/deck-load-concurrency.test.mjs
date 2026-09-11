/**
 * Was this deck load measured alone? The instrument that answers it.
 *
 * THE POINT OF THIS SUITE, stated so a later reader does not weaken it: a
 * contention detector in which every case comes back `solo=1` is
 * indistinguishable from one wired to a constant, from one pointed at the
 * wrong spans, and from one that never ran. So the first two cases below are a
 * MATCHED PAIR over the same span list shape -- one that MUST report solo, one
 * that MUST report contended -- and neither is allowed to pass without the
 * other. That is the presence-of-the-good-thing rule in .claude/rules/
 * verification.md applied to the instrument rather than to its subject.
 *
 * Every case drives `concurrencyLabels`, which is the function that actually
 * writes the row's labels, rather than the interval helpers underneath it.
 * A test of an internal that the row does not go through can pass while the
 * row carries something else.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let concurrencyLabels;

/** A finished span. Ids are explicit so a subject is never confused for a peer. */
function span(id, startMs, endMs, deck = 1) {
	return { id, deck, startMs, endMs };
}

before(async () => {
	({ concurrencyLabels } = await loadTypeScriptModule('src/lib/rb/deck-load-concurrency.ts'));
});

test('a load with nothing else in flight reports solo=1', () => {
	const subject = span(1, 0, 100);
	// A neighbour that ran BEFORE and finished before this one began. Present
	// on purpose: an empty peer list would prove only that the function
	// tolerates no peers, not that it can tell a disjoint span from an
	// overlapping one.
	const earlier = span(2, -500, -400);
	const labels = concurrencyLabels(subject, [subject, earlier], 100);
	assert.equal(labels.solo, '1');
	assert.equal(labels.concurrent_loads, '1');
});

test('a load overlapped by another reports solo=0 and the peak count', () => {
	const subject = span(1, 0, 100);
	// Same shape as the case above, moved so it INTERSECTS. This is the half
	// that fails if the overlap test is stubbed, inverted, or reading the
	// wrong field.
	const overlapping = span(2, 50, 400);
	const labels = concurrencyLabels(subject, [subject, overlapping], 400);
	assert.equal(labels.solo, '0');
	assert.equal(labels.concurrent_loads, '2');
});

test('the load that finishes FIRST still sees the load it is racing', () => {
	// The case a naive "reconstruct the interval when the row is written"
	// scheme gets wrong, and the reason spans are registered while in flight:
	// at t=100 the subject is done and its rival has not written a row yet.
	// `endMs: null` is that rival, still running.
	const subject = span(1, 0, 100);
	const stillRunning = span(2, 50, null);
	const labels = concurrencyLabels(subject, [subject, stillRunning], 100);
	assert.equal(labels.solo, '0');
	assert.equal(labels.concurrent_loads, '2');
});

test('a load that merely touches another at one instant counts as contended', () => {
	// Closed intervals, the conservative direction: this can only ever
	// downgrade a row from solo to contended, never promote one.
	const subject = span(1, 100, 200);
	const abutting = span(2, 0, 100);
	assert.equal(concurrencyLabels(subject, [subject, abutting], 200).solo, '0');
});

test('a load entirely before or after another stays solo', () => {
	const subject = span(1, 100, 200);
	const before_ = span(2, 0, 99);
	const after = span(3, 201, 300);
	const labels = concurrencyLabels(subject, [subject, before_, after], 300);
	assert.equal(labels.solo, '1');
	assert.equal(labels.concurrent_loads, '1');
});

test('concurrent_loads is a PEAK, not a total of everything that overlapped', () => {
	// Four peers all overlap the subject, but only two are ever in flight at
	// the same instant as it. A count of overlaps would say 5 here.
	const subject = span(1, 0, 1000);
	const peers = [
		span(2, 10, 100),
		span(3, 200, 300),
		span(4, 400, 500),
		span(5, 600, 700)
	];
	const labels = concurrencyLabels(subject, [subject, ...peers], 1000);
	assert.equal(labels.solo, '0');
	assert.equal(labels.concurrent_loads, '2');
});

test('a genuine four-deck burst reports the real peak', () => {
	const subject = span(1, 0, 1000, 1);
	const peers = [span(2, 100, 900, 2), span(3, 200, 800, 3), span(4, 300, 700, 4)];
	const labels = concurrencyLabels(subject, [subject, ...peers], 1000);
	assert.equal(labels.solo, '0');
	assert.equal(labels.concurrent_loads, '4');
});

test('a peer running across the whole window adds one, not a window-wide count', () => {
	// The clip-to-subject rule: a load that began long before and ended long
	// after contributes exactly one unit of contention to THIS load's window.
	const subject = span(1, 500, 600);
	const enveloping = span(2, 0, 5000);
	assert.equal(concurrencyLabels(subject, [subject, enveloping], 5000).concurrent_loads, '2');
});

test('an instantaneous load is still counted as one, never zero', () => {
	// A zero here would be indistinguishable from "nothing was measured",
	// which is the reading these labels exist to stop a row from making.
	const subject = span(1, 42, 42);
	assert.equal(concurrencyLabels(subject, [subject], 42).concurrent_loads, '1');
});

test('the subject never counts itself twice even when passed in the peer list', () => {
	const subject = span(7, 0, 100);
	const labels = concurrencyLabels(subject, [subject, subject], 100);
	assert.equal(labels.solo, '1');
	assert.equal(labels.concurrent_loads, '1');
});

test('labels are strings, because PerfEvent.labels is a string map', () => {
	// stages is a ms-per-stage map and anything summing or charting it must
	// never meet a value that is not a duration; that is why these live in
	// labels, and why they must arrive as strings.
	const labels = concurrencyLabels(span(1, 0, 10), [span(1, 0, 10)], 10);
	assert.equal(typeof labels.solo, 'string');
	assert.equal(typeof labels.concurrent_loads, 'string');
});
