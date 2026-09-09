/**
 * AUDIOLIVE-06 / issue #1641: the escalation set must not drift below the
 * severity the audio-output modules chose.
 *
 * `audio-output-dead` is the app's ONLY device-level "no sound is leaving this
 * machine" verdict. It was recorded at `error` and absent from
 * `ESCALATED_KINDS`, so it never reached `/api/v1/client-errors`: during the
 * Wed 9 Sep 2026 cutout the browser's own ring could not be read either (13 of
 * 14 WebKit localStorage stores were empty), and every `audio-output-*` row
 * from that session is simply gone.
 *
 * Adding the two names would fix the instance. This checks the CLASS: any kind
 * the audio-output modules record at `error` must be escalated, so the next one
 * cannot be added at `error` and silently stay in the browser.
 *
 * [if] an audio-output module records a new kind at `error` without adding it
 *   to ESCALATED_KINDS [then] this reds [⛔️ if the set can drift one row at a
 *   time again].
 * [if] the scanner stops matching real emit sites [then] it fails loudly rather
 *   than reporting an empty set as a pass [⛔️ if zero findings read as clean -
 *   the .claude/rules/verification.md failure this file exists to avoid].
 * [if] a kind is escalated that no module records at error [then] that is
 *   allowed: the set is a SUPERSET, not an equality.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

/** Modules whose perf rows mean "the operator may be hearing nothing". */
const AUDIO_OUTPUT_MODULES = [
	'../../src/lib/rb/audio-output-liveness.ts',
	'../../src/lib/rb/audio-output-rebind.ts'
];

const PERF_EVENT_LOG = fileURLToPath(
	new URL('../../src/lib/rb/perf-event-log.ts', import.meta.url)
);

/**
 * Every kind literal passed to a recordPerfEvent call whose severity argument
 * is 'error'. The call spans several lines, so the match runs over the whole
 * argument list rather than one line at a time.
 */
const ERROR_EMIT = /recordPerfEvent\(\s*'([a-z0-9-]+)'\s*,[\s\S]{0,400}?'error'\s*\)/g;

function errorKindsIn(source) {
	return new Set([...source.matchAll(ERROR_EMIT)].map((match) => match[1]));
}

function escalatedKinds() {
	const source = readFileSync(PERF_EVENT_LOG, 'utf8');
	const start = source.indexOf('const ESCALATED_KINDS');
	assert.notEqual(start, -1, 'ESCALATED_KINDS could not be located: this guard asserts nothing');
	const end = source.indexOf(']);', start);
	assert.notEqual(end, -1, 'ESCALATED_KINDS is not a closed literal: this guard asserts nothing');
	const kinds = new Set(
		[...source.slice(start, end).matchAll(/'([a-z0-9-]+)'/g)].map((match) => match[1])
	);
	assert.ok(kinds.size > 0, 'parsed an EMPTY escalation set, which would pass any superset check');
	return kinds;
}

test('the scanner actually finds emit sites (instrument control)', () => {
	const found = new Set();
	for (const path of AUDIO_OUTPUT_MODULES) {
		for (const kind of errorKindsIn(readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8'))) {
			found.add(kind);
		}
	}
	// Derived from the claim under test, not from unrelated material: the
	// hypothesis is "these modules record device-death rows at error", so these
	// are the rows it predicts. A zero here would otherwise render as a pass.
	assert.ok(found.has('audio-output-dead'), `scanner found ${[...found].join(', ') || 'nothing'}`);
	assert.ok(found.has('audio-output-dead-persistent'));
	assert.ok(found.has('audio-output-rebind-failed'));
});

test('the scanner reports a NEW error kind as missing (negative control)', () => {
	const injected = errorKindsIn(
		"effects.recordPerfEvent(\n'audio-output-invented',\n'a kind nobody escalated',\n'error'\n);"
	);
	assert.deepEqual([...injected], ['audio-output-invented']);
	assert.ok(
		!escalatedKinds().has('audio-output-invented'),
		'a kind absent from the set must report absent, loudly'
	);
});

test('the scanner ignores rows recorded below error (negative control)', () => {
	const info = errorKindsIn(
		"effects.recordPerfEvent('audio-output-rebound', `output re-bound after ${reason}`, 'info');"
	);
	assert.equal(info.size, 0, 'escalating info rows would flood the wire during a set');
});

test('ESCALATED_KINDS is a superset of every audio-output kind recorded at error', () => {
	const escalated = escalatedKinds();
	const missing = [];
	for (const path of AUDIO_OUTPUT_MODULES) {
		const source = readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8');
		for (const kind of errorKindsIn(source)) {
			if (!escalated.has(kind)) missing.push(`${path}: ${kind}`);
		}
	}
	assert.deepEqual(
		missing,
		[],
		'these kinds mean the operator is hearing nothing and never leave the browser'
	);
});
