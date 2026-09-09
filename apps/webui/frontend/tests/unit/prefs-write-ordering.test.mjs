import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Split out of prefs-api.test.mjs (issue #1578) once a second real-http-
// transport test pushed that file over the 600-line quality gate ceiling -
// both tests belong together: same recorder pattern, same write-ordering
// concern, just different sets of setters racing each other.

let originalFetch;

before(() => {
	originalFetch = globalThis.fetch;
});

function installPrefsStorage(raw = null) {
	const values = new Map(raw === null ? [] : [['mdt.rb.ui-prefs.v1', raw]]);
	const originalWindow = globalThis.window;
	globalThis.window = {
		localStorage: {
			getItem: (key) => values.get(key) ?? null,
			setItem: (key, value) => values.set(key, value)
		}
	};
	return {
		values,
		restore: () => {
			if (originalWindow === undefined) delete globalThis.window;
			else globalThis.window = originalWindow;
		}
	};
}

/**
 * A real `node:http` server that records each PUT body and holds its
 * response open until `releases.shift()()` is called, so a parallel
 * implementation would actually overlap two in-flight requests rather than
 * merely racing microtasks. Real transport, not a `globalThis.fetch` stub:
 * a fabricated transport success cannot evidence request ORDERING
 * (AGENTS.md, "No mocks and locked real fixtures"). The server is a
 * recorder, not a stand-in for anything under test - the behaviour being
 * pinned is entirely client-side write ordering.
 */
async function createRecordingServer() {
	const bodies = [];
	const releases = [];
	let inFlight = 0;
	let maxInFlight = 0;

	const server = createServer((req, res) => {
		let raw = '';
		req.on('data', (c) => (raw += c));
		req.on('end', () => {
			inFlight += 1;
			maxInFlight = Math.max(maxInFlight, inFlight);
			bodies.push(JSON.parse(raw));
			releases.push(() => {
				inFlight -= 1;
				res.writeHead(200, { 'content-type': 'application/json' });
				res.end(JSON.stringify({ theme: 'dark' }));
			});
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));

	return {
		base: `http://127.0.0.1:${server.address().port}`,
		bodies,
		maxInFlightRef: () => maxInFlight,
		async releaseInOrder(count) {
			for (let i = 0; i < count; i += 1) {
				const deadline = Date.now() + 10_000;
				while (releases.length === 0) {
					if (Date.now() > deadline) throw new Error(`write ${i + 1} never reached the server`);
					await new Promise((r) => setTimeout(r, 5));
				}
				releases.shift()();
				await new Promise((r) => setTimeout(r, 20));
			}
		},
		close: () => new Promise((resolve) => server.close(resolve))
	};
}

// Codex P2 then P1 BLOCKING on #1503: each R/M action PUTs a full snapshot of
// BOTH halves, so two quick clicks could land out of order and an older
// snapshot could overwrite a newer one. The tab looked right; the lost toggle
// came back after a reload.
//
// Regression line: if calibration writes stop being chained then the LAST
// click is not the last write, and a capture silently reverts on reload.
test('calibration writes are serialized over a real http transport', async () => {
	const recorder = await createRecordingServer();

	// Earlier tests elsewhere may replace globalThis.fetch and not all restore
	// it. This test needs the REAL fetch or it silently talks to a leftover
	// stub instead of the server above, which is how it first failed.
	globalThis.fetch = originalFetch;
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', {
			viteApiBase: recorder.base
		});

		isolated.setLevelCalibrationCapture('red', -12);
		isolated.setLevelCalibrationCapture('ceiling', -3);
		isolated.setLevelCalibrationDisabled('red');

		await recorder.releaseInOrder(3);

		assert.equal(
			recorder.maxInFlightRef(),
			1,
			`writes overlapped (${recorder.maxInFlightRef()} in flight at once)`
		);
		assert.equal(recorder.bodies.length, 3, 'every action must still reach the daemon');
		const last = recorder.bodies[recorder.bodies.length - 1].level_calibration;
		assert.equal(last.red_enabled, false, 'last write lost the disable');
		assert.equal(last.ceiling_dbfs, -3, 'last write lost the ceiling capture');
	} finally {
		storage.restore();
		await recorder.close();
	}
});

// Issue #1578: the chain above only ever serialized calibration writes against
// EACH OTHER. Every other setter in prefs.svelte.ts (setTheme, deck-layout,
// setShowAgentPins, ...) called _syncDiskPrefs directly, on no queue at all,
// so a calibration write and any other pref write in the same tick raced each
// other exactly like two calibration writes used to. Fixed by hoisting the
// chain into prefs.svelte.ts's own _syncDiskPrefs so every disk write, from
// every setter, shares ONE queue.
//
// Regression line: if a setter is added or changed to bypass _syncDiskPrefs
// (a direct api.PUT call, or its own private chain like the old
// level-calibration-prefs.ts), this goes back to overlapping writes.
test('a calibration write and an unrelated pref write are serialized together', async () => {
	const recorder = await createRecordingServer();

	globalThis.fetch = originalFetch;
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', {
			viteApiBase: recorder.base
		});

		isolated.setLevelCalibrationCapture('red', -12);
		isolated.setTheme('light');
		isolated.setDeckLayoutMode('less');

		await recorder.releaseInOrder(3);

		assert.equal(
			recorder.maxInFlightRef(),
			1,
			`a calibration write raced an unrelated pref write (${recorder.maxInFlightRef()} in flight at once)`
		);
		assert.equal(recorder.bodies.length, 3, 'every action must still reach the daemon');
	} finally {
		storage.restore();
		await recorder.close();
	}
});
