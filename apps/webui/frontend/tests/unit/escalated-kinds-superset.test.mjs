/**
 * AUDIOLIVE-07 / issue #1641: the escalation set must not drift below the
 * severity the audio-output modules chose.
 *
 * `audio-output-dead` is the app's ONLY device-level "no sound is leaving this
 * machine" verdict. It was recorded at `error` and absent from
 * `ESCALATED_KINDS`, so it never reached `/api/v1/client-errors`: during the
 * Wed 9 Sep 2026 cutout the browser's own ring could not be read either (13 of
 * 14 WebKit localStorage stores were empty), and every `audio-output-*` row
 * from that session is simply gone.
 *
 * Adding the two names fixes the instance. This checks the CLASS.
 *
 * HOW IT AVOIDS BEING A REGEX THAT LOOKS AWAY. Every `recordPerfEvent(` call
 * in the scanned modules is located by a balanced-paren scan and split into
 * top-level arguments, and each call must resolve to a quoted-literal kind AND
 * a decidable severity. A call this cannot read - a double-quoted kind, a kind
 * held in a constant, a severity built by an expression - is UNCLASSIFIED and
 * FAILS, rather than being silently omitted from the superset. A measurement
 * that could not be taken must not render as a clean result
 * (.claude/rules/verification.md).
 *
 * [if] an audio-output module records a new kind at `error` without adding it
 *   to ESCALATED_KINDS [then] this reds ⛔️
 * [if] a call is written in a form the scanner cannot decide [then] it reds
 *   naming that call, instead of reporting a pass over a shrunken set ⛔️
 * [if] the scanner stops matching real emit sites [then] the instrument control
 *   reds ⛔️
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

const CALL = 'recordPerfEvent(';

/** Index just past the `)` that closes the call opening at `open`. */
function endOfCall(source, open) {
	let depth = 0;
	let quote = null;
	for (let i = open; i < source.length; i += 1) {
		const char = source[i];
		if (quote !== null) {
			if (char === '\\') i += 1;
			else if (char === quote) quote = null;
			continue;
		}
		if (char === "'" || char === '"' || char === '`') quote = char;
		else if (char === '(') depth += 1;
		else if (char === ')') {
			depth -= 1;
			if (depth === 0) return i;
		}
	}
	return -1;
}

/** Top-level comma-separated arguments, quotes and nesting respected. */
function splitArgs(inner) {
	const args = [];
	let depth = 0;
	let quote = null;
	let start = 0;
	for (let i = 0; i < inner.length; i += 1) {
		const char = inner[i];
		if (quote !== null) {
			if (char === '\\') i += 1;
			else if (char === quote) quote = null;
			continue;
		}
		if (char === "'" || char === '"' || char === '`') quote = char;
		else if (char === '(' || char === '[' || char === '{') depth += 1;
		else if (char === ')' || char === ']' || char === '}') depth -= 1;
		else if (char === ',' && depth === 0) {
			args.push(inner.slice(start, i).trim());
			start = i + 1;
		}
	}
	args.push(inner.slice(start).trim());
	return args.filter((arg) => arg.length > 0);
}

const QUOTED = /^'([^']*)'$|^"([^"]*)"$/;

function literal(arg) {
	const match = QUOTED.exec(arg);
	if (match === null) return null;
	return match[1] ?? match[2];
}

/**
 * Classify every recordPerfEvent call in one module.
 *
 * Returns { error, other, unclassified }. `unclassified` is the honest bucket:
 * a call whose kind or severity cannot be READ from the source, which the
 * caller must treat as a failure rather than as nothing to report.
 */
export function classifyCalls(source, label = 'source') {
	const error = new Set();
	const other = new Set();
	const unclassified = [];
	let from = 0;
	for (;;) {
		const at = source.indexOf(CALL, from);
		if (at === -1) break;
		const open = at + CALL.length - 1;
		from = at + CALL.length;
		const close = endOfCall(source, open);
		if (close === -1) {
			unclassified.push(`${label}: unterminated call at offset ${at}`);
			continue;
		}
		const inner = source.slice(open + 1, close);
		// An interface member declares `recordPerfEvent(kind: string, ...)`.
		// That is a type, not an emit site, and it is the one shape allowed to
		// carry a non-literal first argument.
		if (/^\s*\w+\s*:/.test(inner)) continue;
		const args = splitArgs(inner);
		const kind = args.length === 0 ? null : literal(args[0]);
		if (kind === null) {
			unclassified.push(`${label}: kind is not a quoted literal in ${CALL}${inner.slice(0, 60)}`);
			continue;
		}
		// Severity is the last argument when it is a quoted literal; a call with
		// only kind+message defaults to a non-error severity at the call site's
		// own signature. Anything else is undecidable from here.
		const last = args.length > 2 ? literal(args[args.length - 1]) : null;
		if (args.length > 2 && last === null) {
			unclassified.push(`${label}: severity is not a quoted literal for kind ${kind}`);
			continue;
		}
		if (last === 'error') error.add(kind);
		else other.add(kind);
	}
	return { error, other, unclassified };
}

function moduleSource(relative) {
	const source = readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');
	assert.ok(source.length > 0, `${relative} is empty: this guard would assert nothing`);
	return source;
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
	const error = new Set();
	for (const path of AUDIO_OUTPUT_MODULES) {
		for (const kind of classifyCalls(moduleSource(path), path).error) error.add(kind);
	}
	// Derived from the claim under test, not from unrelated material: the
	// hypothesis is "these modules record device-death rows at error", so these
	// are the rows it predicts. A zero here would otherwise render as a pass.
	assert.ok(error.has('audio-output-dead'), `scanner found ${[...error].join(', ') || 'nothing'}`);
	assert.ok(error.has('audio-output-dead-persistent'));
	assert.ok(error.has('audio-output-rebind-failed'));
});

test('the scanner reads forms a line regex would miss', () => {
	const doubleQuoted = classifyCalls(
		'effects.recordPerfEvent("audio-output-invented", "a new kind", "error");'
	);
	assert.deepEqual([...doubleQuoted.error], ['audio-output-invented'], 'double quotes are read');

	const spreadOverLines = classifyCalls(
		[
			'effects.recordPerfEvent(',
			"\t'audio-output-verbose',",
			'\t`a message so long that a bounded lookahead gives up: ' + 'x'.repeat(600) + '`,',
			"\t'error'",
			');'
		].join('\n')
	);
	assert.deepEqual(
		[...spreadOverLines.error],
		['audio-output-verbose'],
		'a long message argument must not hide the severity'
	);

	const nestedCommas = classifyCalls(
		"effects.recordPerfEvent('audio-output-nested', fmt(a, b), 'error');"
	);
	assert.deepEqual([...nestedCommas.error], ['audio-output-nested'], 'nested commas are not splits');
});

test('a call the scanner cannot decide is UNCLASSIFIED, not skipped', () => {
	const constantKind = classifyCalls("effects.recordPerfEvent(KIND, 'msg', 'error');", 'synthetic');
	assert.equal(constantKind.error.size, 0);
	assert.equal(constantKind.other.size, 0);
	assert.equal(constantKind.unclassified.length, 1, 'a constant kind must be reported, not dropped');

	const computedSeverity = classifyCalls(
		"effects.recordPerfEvent('audio-output-x', 'msg', bad ? 'error' : 'info');",
		'synthetic'
	);
	assert.equal(computedSeverity.unclassified.length, 1, 'a computed severity is undecidable here');
});

test('the scanner ignores rows recorded below error, and interface declarations', () => {
	const info = classifyCalls(
		"effects.recordPerfEvent('audio-output-rebound', `output re-bound after ${reason}`, 'info');"
	);
	assert.equal(info.error.size, 0, 'escalating info rows would flood the wire during a set');
	assert.deepEqual([...info.other], ['audio-output-rebound']);

	const declaration = classifyCalls(
		"recordPerfEvent(kind: string, message: string, severity: 'info' | 'error'): void;"
	);
	assert.equal(declaration.unclassified.length, 0, 'a type signature is not an emit site');
	assert.equal(declaration.error.size + declaration.other.size, 0);
});

test('every audio-output call is readable, and none is unescalated', () => {
	const escalated = escalatedKinds();
	const unreadable = [];
	const missing = [];
	let calls = 0;
	for (const path of AUDIO_OUTPUT_MODULES) {
		const found = classifyCalls(moduleSource(path), path);
		unreadable.push(...found.unclassified);
		calls += found.error.size + found.other.size;
		for (const kind of found.error) if (!escalated.has(kind)) missing.push(`${path}: ${kind}`);
	}
	assert.deepEqual(unreadable, [], 'an undecidable emit site makes this whole check unsound');
	assert.ok(calls >= 4, `only ${calls} emit sites read: the scanner is looking at the wrong thing`);
	assert.deepEqual(
		missing,
		[],
		'these kinds mean the operator is hearing nothing and never leave the browser'
	);
});
