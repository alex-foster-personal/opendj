import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * P0 (audio-never-cuts-out-under-thrash, defect D4): a perf event nobody can
 * read from outside the browser is not a record of anything.
 *
 * `recordPerfEvent` writes to console, an in-memory ring, and LocalStorage
 * `mdt.perfEventLog`. That is where it stops. `reportClientError`
 * (src/lib/client-error-reporting.ts) POSTs to `/api/v1/client-errors`, has a
 * durable retry queue, and is already wired into `hooks.client.ts` and
 * `stores.svelte.ts` - but NO perf event of any severity is ever handed to it.
 *
 * So on Wed 2 Sep 2026 the only durable trace of a 24-minute audio outage
 * would have lived in the LocalStorage of the browser profile that dropped
 * out: the one place nobody looks mid-set, and the one place a profile reset
 * destroys. The whole point of a hardening instrument is that somebody other
 * than the person in the room can see it afterwards.
 *
 * THE SINK IS INJECTED, and the reason is a CI failure worth remembering.
 * These tests originally asserted on the POST, which meant perf-event-log had
 * to `import { reportClientError }` statically. That import reaches
 * `$lib/api/client.ts`, which evaluates `import.meta.env.VITE_API_BASE` at
 * module scope, and Playwright loads spec files under plain Node where
 * `import.meta.env` is undefined. Every Playwright config whose specs reached
 * this module transitively then died at CONFIG LOAD - `TypeError: Cannot read
 * properties of undefined (reading 'VITE_API_BASE')` followed by `Error: No
 * tests found`, so the gate reported ZERO TESTS rather than a failure, which
 * is the worst shape a broken gate can take.
 *
 * So perf-event-log is import-free again and takes its sink through
 * `setPerfEventEscalator`. These tests inject a fake sink; the guard test at
 * the bottom is what stops the static import coming back.
 *
 * Regression lines:
 * - if an error-severity audio event never leaves the browser then the only
 *   record of an outage dies with the profile that suffered it
 * - if info/warn rows are forwarded too then one PitchFader drag (which emits
 *   a transport-schedule row per pointermove) floods /api/v1/client-errors
 */

/**
 * The audio-liveness kinds. All of them must escalate at error severity.
 *
 * `audio-output-rebind-failed` joined on Wed 9 Sep 2026. The whole output-rebind
 * path was wired at `info`, so the row that means "the output device changed,
 * the recovery ran, and it did NOT work" stayed inside the browser - which is
 * exactly why the 17:44:43Z Bluetooth-flap cutout on the Air left nothing at all
 * in `webui-client-errors-2026-09-09.log`.
 */
const ESCALATING_KINDS = [
	'xrun',
	'audio-context',
	'silent-while-playing',
	'audio-output-rebind-failed'
];

let perfLog;
/** Every row handed to the sink, in order. */
let escalated;

before(async () => {
	perfLog = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
});

beforeEach(() => {
	escalated = [];
	perfLog.setPerfEventEscalator((event) => escalated.push(event));
});

//-----------------------------------------------------------------------------
// what must escalate
//-----------------------------------------------------------------------------

for (const kind of ESCALATING_KINDS) {
	test(`an error-severity ${kind} event is escalated off this browser`, () => {
		const message = `${kind} escalation probe ${Math.random().toString(36).slice(2)}`;
		perfLog.recordPerfEvent(kind, message, null, 'error');
		const hit = escalated.find((event) => event.message === message);
		assert.ok(
			hit !== undefined,
			`if an error-severity '${kind}' perf event is not handed to the escalator then ` +
				'broken - the row exists only in the LocalStorage of the browser profile that ' +
				'failed, so a 24-minute audio outage leaves nothing anybody else can read ' +
				`(escalated: ${JSON.stringify(escalated.map((e) => e.kind))})`
		);
		assert.equal(
			hit.kind,
			kind,
			'if the forwarded row does not carry the perf kind then broken - the engine ' +
				'cannot tell an audio dropout from any other client error'
		);
	});
}

test('the boot wiring really points the sink at reportClientError', () => {
	// The injected sink above proves the MECHANISM. This proves the PRODUCTION
	// wiring exists, which the fake can never show: without it, every test here
	// passes against an app that escalates nothing.
	const boot = readFrontendSource('src/lib/client-error-reporting.ts');
	assert.ok(
		boot.includes('setPerfEventEscalator('),
		'if nothing calls setPerfEventEscalator at boot then broken - the sink stays null ' +
			'in the real app and audio-liveness failures never leave the browser'
	);
	assert.ok(
		boot.slice(boot.indexOf('setPerfEventEscalator(')).includes('reportClientError('),
		'if the sink is wired to something other than reportClientError then broken - only ' +
			'that path carries the durable retry queue and the POST to /api/v1/client-errors'
	);
	assert.ok(
		readFrontendSource('src/hooks.client.ts').includes('installClientErrorReporting()'),
		'if client boot stops calling installClientErrorReporting then the wiring above ' +
			'never runs, however correct it is'
	);
});

//-----------------------------------------------------------------------------
// what must NOT escalate
//-----------------------------------------------------------------------------

test('CONTROL: warn and info severity rows stay local', () => {
	// transport-schedule is appended once per _scheduleDeck and PitchFader drives
	// that from an unthrottled pointermove, so "forward every perf event" would
	// put ~40 POSTs on the wire per fader drag, during a set, on the same main
	// thread as the audio this P0 is trying to protect.
	perfLog.recordPerfEvent('transport-schedule', 'routine schedule row', 2, 'warn');
	perfLog.recordPerfEvent('deck-load', 'routine load row', 1, 'info');
	assert.equal(
		escalated.length,
		0,
		'if a warn/info perf row is escalated then broken - one PitchFader drag would flood ' +
			`/api/v1/client-errors (escalated: ${JSON.stringify(escalated.map((e) => e.kind))})`
	);
});

test('a sustained condition escalates once per window, not once per report', () => {
	// The xrun sentinel reports every 2s for as long as the machine struggles, so
	// a twenty-minute incident is ~600 reports describing the same twenty minutes.
	const kind = 'presentation-clock-stalled';
	for (let report = 0; report < 25; report += 1) {
		perfLog.recordPerfEvent(kind, `stall report ${report}, worst gap ${report}ms`, null, 'error');
	}
	assert.equal(
		escalated.length,
		1,
		`if a sustained condition escalates ${escalated.length} times then broken - one ` +
			'incident must not become one round trip per report, on the same main thread as ' +
			'the audio'
	);
});

test('CONTROL: the ring still records the row it escalated', () => {
	// Escalation must be additive. If it ever routes the row to the engine INSTEAD
	// of the ring, __mdtPerfLog() and every toast correlation id go dark.
	const message = `ring retention probe ${Math.random().toString(36).slice(2)}`;
	perfLog.recordPerfEvent('xrun', message, null, 'error');
	assert.ok(
		perfLog.readPerfEvents().some((row) => row.message === message),
		'if escalating a row drops it from the local ring then broken - the ring is what ' +
			'__mdtPerfLog() and every toast correlation id read'
	);
});

test('with no sink wired, an error row is kept locally and warns once, never throws', () => {
	// The null-sink state is legitimate, not a fault: unit tests, Playwright's
	// Node loader and all pre-boot code run without one. A logger that threw its
	// way out of a dropout would be the dropout's second casualty.
	const warnings = [];
	const realWarn = console.warn;
	console.warn = (...args) => warnings.push(args.join(' '));
	try {
		perfLog.setPerfEventEscalator(null);
		for (let i = 0; i < 5; i += 1) {
			assert.doesNotThrow(
				() => perfLog.recordPerfEvent('xrun', `unwired probe ${i}`, null, 'error'),
				'if a missing escalator throws then broken - a logger must never become the ' +
					'failure it is reporting'
			);
		}
	} finally {
		console.warn = realWarn;
	}
	assert.ok(
		perfLog.readPerfEvents().some((row) => row.message === 'unwired probe 4'),
		'if an unwired row is not kept locally then broken - the ring is the last resort ' +
			'precisely when the network path is the thing that is missing'
	);
	assert.equal(
		warnings.length,
		1,
		`if a missing sink warns ${warnings.length} times then broken - a sustained dropout ` +
			'would turn one missing wire into its own console flood'
	);
});

//-----------------------------------------------------------------------------
// the import that broke CI, and must not come back
//-----------------------------------------------------------------------------

test('the pure rb modules import nothing from the API layer', () => {
	// SOURCE-LEVEL, and it has to be: what is being asserted is the absence of a
	// STATIC IMPORT, which is a property of the module text. By the time a test
	// could observe it at runtime the import has already been resolved, and under
	// Playwright's Node loader resolving it is precisely the crash.
	//
	// Wed 2 Sep 2026: perf-event-log.ts gained
	// `import { reportClientError } from '$lib/client-error-reporting'`, which
	// reaches $lib/api/client.ts, which reads import.meta.env.VITE_API_BASE at
	// module scope. Playwright loads spec files under plain Node, so the
	// stretch-quality config died at CONFIG LOAD reporting "No tests found" -
	// zero tests, not a failure, which is the worst way for a gate to break.
	const PURE = [
		'src/lib/rb/perf-event-log.ts',
		'src/lib/rb/xrun-math.ts',
		'src/lib/player/transport/presentation-stall.ts'
	];
	const FORBIDDEN = ['$lib/api', '$lib/client-error-reporting', 'api-rb'];
	for (const modulePath of PURE) {
		const source = readFrontendSource(modulePath);
		const imports = source
			.split('\n')
			.filter((line) => /^\s*import\s/.test(line) || /^\s*}\s*from\s/.test(line));
		for (const forbidden of FORBIDDEN) {
			assert.ok(
				!imports.some((line) => line.includes(forbidden)),
				`if a pure rb module imports the API client then every Playwright config that ` +
					`loads it under Node dies at config load (${modulePath} imports ${forbidden})`
			);
		}
	}
});
