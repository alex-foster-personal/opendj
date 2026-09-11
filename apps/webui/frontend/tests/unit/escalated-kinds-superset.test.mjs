/**
 * INVARIANT: a perf event recorded at `error` severity leaves the browser.
 *
 * WHAT THIS REPLACED, and why. Until Thu 10 Sep 2026 this file compared an
 * eight-name `ESCALATED_KINDS` allowlist in perf-event-log.ts against the kinds
 * recorded by TWO named modules. Its own docstring claimed it "reds if any
 * module records a kind at error severity that is absent from this set". It
 * read `audio-output-liveness.ts` and `audio-output-rebind.ts` and nothing
 * else.
 *
 * A guard whose docstring says "any module" and whose code says "these two"
 * cannot fail for the case it claims to cover, and there was a live escapee:
 * `presentation-tick-failed`, recorded at `error` by
 * presentation-clock-report.ts with a message saying the waveform would have
 * frozen over live audio, was silently dropped before it ever reached the
 * server log. The set also contained `presentation-stalled`, emitted by
 * nothing, and the one-directional comparison could not see that either.
 *
 * A rule that pins VALUES must be maintained forever and rots between
 * maintenances. A rule that pins an INVARIANT cannot go stale. The allowlist
 * is gone, so this file now tests the behaviour directly: record a kind that
 * has never existed, at error severity, and require that it escalates. No list
 * can drift out of step with that, because there is no list.
 *
 * Regression lines:
 *   - if a NOVEL kind recorded at error severity does not escalate then broken
 *     (that is the allowlist coming back)
 *   - if a warn-severity row escalates then broken (the severity gate is the
 *     only gate, and it must still gate)
 *   - if an escalator that throws loses the local ring row then broken
 *   - if perf-event-log.ts regains a kind-keyed VALUE allowlist in the escalate
 *     path then broken
 *   - if a `toast-*` row escalates then broken (pushToast already sends a
 *     richer report for it; two reports of one failure cannot merge and the
 *     in-flight queue write can erase the better one)
 *   - if a NON-toast row stops escalating then broken (that is the overshoot
 *     of the line above, and nothing in the report that produced it objects)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PERF_EVENT_LOG = fileURLToPath(
	new URL('../../src/lib/rb/perf-event-log.ts', import.meta.url)
);

describe('error-severity perf events escalate', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
	});

	it('escalates a kind that has never existed before', () => {
		const seen = [];
		mod.setPerfEventEscalator((event) => seen.push(event));
		// Deliberately absurd: no allowlist could ever contain this, so if it
		// escalates, escalation is not allowlist-gated.
		mod.recordPerfEvent('a-kind-invented-by-this-test', 'novel failure', null, 'error');
		mod.setPerfEventEscalator(null);
		assert.equal(seen.length, 1, 'an error-severity row must leave the browser regardless of its kind');
		assert.equal(seen[0].kind, 'a-kind-invented-by-this-test');
	});

	it('escalates the kind that was silently dropped by the old allowlist', () => {
		const seen = [];
		mod.setPerfEventEscalator((event) => seen.push(event));
		mod.recordPerfEvent('presentation-tick-failed', 'waveform would have frozen', null, 'error');
		mod.setPerfEventEscalator(null);
		assert.deepEqual(seen.map((e) => e.kind), ['presentation-tick-failed']);
	});

	it('does NOT escalate warn severity', () => {
		const seen = [];
		mod.setPerfEventEscalator((event) => seen.push(event));
		mod.recordPerfEvent('some-warning', 'not worth the round trip', null, 'warn');
		mod.setPerfEventEscalator(null);
		assert.equal(seen.length, 0, 'the severity gate must still be a gate');
	});

	it('keeps the local ring row when the escalator throws', () => {
		mod.setPerfEventEscalator(() => {
			throw new Error('network is the thing that is broken');
		});
		let threw = false;
		try {
			mod.recordPerfEvent('escalator-throws', 'local record must survive', null, 'error');
		} catch {
			threw = true;
		}
		mod.setPerfEventEscalator(null);
		assert.equal(threw, false, 'a failing escalator must not propagate');
		const kinds = mod.readPerfEvents().map((row) => row.kind);
		assert.ok(
			kinds.includes('escalator-throws'),
			'the ring is the last resort when the network is broken; it must hold the row'
		);
	});

	it('has no kind-keyed VALUE allowlist left in the escalate path', () => {
		const body = readFileSync(PERF_EVENT_LOG, 'utf8');
		const escalate = body.slice(body.indexOf('function _escalate('));
		const fnEnd = escalate.indexOf('\n}\n');
		const source = escalate.slice(0, fnEnd);
		assert.ok(
			!/ESCALATED_KINDS|\.has\(entry\.kind\)/.test(source),
			`_escalate must not consult a set of kind VALUES; such a list can only ever ` +
				`drop error-severity rows before they leave the browser:\n${source}`
		);
	});

	// ---- exactly once, not zero and not twice ---------------------------
	//
	// `pushToast(msg, 'error')` writes the `toast-error` ring row AND sends its
	// own reportClientError carrying the real cause, the toast id and any
	// context the caller measured. Escalating the ring row too produces two
	// reports of one failure that cannot merge (reportClientError fingerprints
	// on kind:source:message and both halves differ), and the second write can
	// be erased by the first flush writing back its pre-await snapshot.
	//
	// Both directions are asserted, because the overshoot -- suppressing rows
	// generally -- satisfies the finding perfectly and would silence every
	// audio fault again.

	it('does NOT escalate a toast row: pushToast already reports it', () => {
		const seen = [];
		mod.setPerfEventEscalator((event) => seen.push(event));
		mod.recordPerfEvent('toast-error', 'deck 1 failed to load', null, 'error');
		mod.setPerfEventEscalator(null);
		assert.deepEqual(
			seen.map((e) => e.kind),
			[],
			'an error toast must take ONE trip to the server, the context-rich one pushToast sends'
		);
	});

	it('still keeps the toast row in the local ring', () => {
		mod.recordPerfEvent('toast-error', 'the ring is not the thing being skipped', null, 'error');
		const kinds = mod.readPerfEvents().map((row) => row.kind);
		assert.ok(kinds.includes('toast-error'), 'not escalating a row must not stop recording it');
	});

	it('escalates an audio fault, which has no other reporter', () => {
		const seen = [];
		mod.setPerfEventEscalator((event) => seen.push(event));
		mod.recordPerfEvent('audio-output-dead', 'rendering into a dead output', null, 'error');
		mod.setPerfEventEscalator(null);
		assert.deepEqual(
			seen.map((e) => e.kind),
			['audio-output-dead'],
			'suppressing rows generally is the overshoot of the toast rule and re-hides the outage'
		);
	});

	it('escalates a kind that merely CONTAINS toast, rather than being one', () => {
		const seen = [];
		mod.setPerfEventEscalator((event) => seen.push(event));
		mod.recordPerfEvent('deck-toast-bridge-failed', 'not a toast row', null, 'error');
		mod.setPerfEventEscalator(null);
		assert.deepEqual(
			seen.map((e) => e.kind),
			['deck-toast-bridge-failed'],
			'the rule is a kind PREFIX naming who writes the row, not a substring'
		);
	});
});
