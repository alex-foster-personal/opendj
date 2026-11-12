/**
 * INSTALL-21 quit confirmation: needsQuitConfirmation and dialog decisions.
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { fixtureServerLaunch } from '../../scripts/fixture-server.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPO_ROOT = resolve(FRONTEND_ROOT, '../../..');
const QUIT_GATE_SERVER_START_TIMEOUT_MS = 90_000;

let needsQuitConfirmation;
let planQuitRequest;
let isQuitConfirmOpen;
let openQuitConfirm;
let closeQuitConfirm;
let confirmQuit;
let ensureEarlyShellQuitGate;
let resetShellQuitGateForTests;

/** Boot the throwaway real engine (WS hub + lifecycle/quit). */
function startQuitGateTestServer() {
	return new Promise((resolvePort, reject) => {
		let launch;
		try {
			launch = fixtureServerLaunch({
				repoRoot: REPO_ROOT,
				pythonTarget: 'scripts.testing.quit_gate_test_server',
				pythonTargetIsModule: true,
				extraArgs: ['--port', '0']
			});
		} catch (error) {
			reject(error);
			return;
		}
		const proc = spawn(launch.command, launch.args, {
			cwd: launch.cwd,
			env: launch.env,
			stdio: ['ignore', 'pipe', 'pipe']
		});
		const timer = setTimeout(() => {
			proc.kill();
			reject(
				new Error(
					`quit_gate_test_server did not report LISTENING within ${QUIT_GATE_SERVER_START_TIMEOUT_MS}ms`
				)
			);
		}, QUIT_GATE_SERVER_START_TIMEOUT_MS);
		proc.on('error', (error) => {
			clearTimeout(timer);
			reject(error);
		});
		let stderr = '';
		proc.stderr.on('data', (chunk) => {
			stderr += String(chunk);
		});
		proc.on('exit', (code) => {
			if (code !== null && code !== 0) {
				clearTimeout(timer);
				reject(new Error(`quit_gate_test_server exited ${code}:\n${stderr}`));
			}
		});
		const lines = createInterface({ input: proc.stdout });
		lines.on('line', (line) => {
			const match = /^LISTENING (\d+)$/.exec(line);
			if (!match) return;
			clearTimeout(timer);
			lines.close();
			resolvePort({ proc, port: Number(match[1]) });
		});
	});
}

function waitForEventsBusOpen(eventsBus, timeoutMs = 15_000) {
	if (eventsBus.getConnectionState() === 'open') return Promise.resolve();
	return new Promise((resolve, reject) => {
		const timer = setTimeout(() => {
			unsub();
			reject(new Error(`events bus did not reach 'open' within ${timeoutMs}ms`));
		}, timeoutMs);
		const unsub = eventsBus.subscribeConnectionState((state) => {
			if (state !== 'open') return;
			clearTimeout(timer);
			unsub();
			resolve();
		});
	});
}

async function waitForQuitCalls(readCount, expected, timeoutMs = 15_000) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (readCount() >= expected) return;
		await new Promise((resolve) => setTimeout(resolve, 10));
	}
	throw new Error(`expected ${expected} quit call(s) within ${timeoutMs}ms, got ${readCount()}`);
}

function deck(overrides = {}) {
	return {
		stable_id: null,
		playing: false,
		audible: false,
		...overrides
	};
}

function state(deckOverrides = {}) {
	return {
		decks: {
			1: deck(deckOverrides[1] ?? {}),
			2: deck(deckOverrides[2] ?? {}),
			3: deck(deckOverrides[3] ?? {}),
			4: deck(deckOverrides[4] ?? {})
		}
	};
}

before(async () => {
	const needs = await loadTypeScriptModule('src/lib/shell/needs-quit-confirmation.ts');
	needsQuitConfirmation = needs.needsQuitConfirmation;
	const logic = await loadTypeScriptModule('src/lib/shell/quit-gate-logic.ts');
	planQuitRequest = logic.planQuitRequest;
	const overlay = await loadTypeScriptModule('src/lib/shell/quit-gate-state.ts');
	isQuitConfirmOpen = overlay.isQuitConfirmOpen;
	openQuitConfirm = overlay.openQuitConfirm;
	closeQuitConfirm = overlay.closeQuitConfirm;
	const gate = await loadTypeScriptModule('src/lib/shell/quit-gate.ts');
	confirmQuit = gate.confirmQuit;
	ensureEarlyShellQuitGate = gate.ensureEarlyShellQuitGate;
	resetShellQuitGateForTests = gate.resetShellQuitGateForTests;
	closeQuitConfirm();
});

test('needsQuitConfirmation is false only when empty and silent', () => {
	assert.equal(needsQuitConfirmation(state()), false);
});

test('needsQuitConfirmation is true when a deck is loaded but paused', () => {
	assert.equal(needsQuitConfirmation(state({ 1: { stable_id: 'track-001' } })), true);
});

test('needsQuitConfirmation is true when a deck is playing', () => {
	assert.equal(needsQuitConfirmation(state({ 2: { playing: true } })), true);
});

test('needsQuitConfirmation is true when a deck is audible only', () => {
	assert.equal(needsQuitConfirmation(state({ 3: { audible: true } })), true);
});

test('first quit request opens the dialog when confirmation is required', () => {
	closeQuitConfirm();
	assert.equal(
		planQuitRequest({
			dialogOpen: false,
			needsConfirmation: needsQuitConfirmation(state({ 1: { stable_id: 'track-001' } }))
		}),
		'open-dialog'
	);
	openQuitConfirm(null);
	assert.equal(isQuitConfirmOpen(), true);
});

test('second quit request confirms when the dialog is already open', () => {
	assert.equal(
		planQuitRequest({
			dialogOpen: true,
			needsConfirmation: true
		}),
		'confirm'
	);
});

test('force quit skips the dialog', () => {
	assert.equal(
		planQuitRequest({
			force: true,
			dialogOpen: false,
			needsConfirmation: true
		}),
		'confirm'
	);
});

test('empty session quits immediately without opening the dialog', () => {
	assert.equal(
		planQuitRequest({
			dialogOpen: false,
			needsConfirmation: needsQuitConfirmation(state())
		}),
		'confirm'
	);
});

test('cancelQuit closes the dialog', () => {
	openQuitConfirm(null);
	closeQuitConfirm();
	assert.equal(isQuitConfirmOpen(), false);
});

test('confirmQuit flushes before exit', async () => {
	const order = [];
	await confirmQuit({
		flushSnapshot: () => {
			order.push('flush');
		},
		exitShell: async () => {
			order.push('exit');
		}
	});
	assert.deepEqual(order, ['flush', 'exit']);
});

test('lifecycle/quit before the events socket opens still force-quits (issue #6262)', async () => {
	let proc;
	let port;
	try {
		({ proc, port } = await startQuitGateTestServer());
		const apiOrigin = `http://127.0.0.1:${port}`;
		const mod = await loadTypeScriptModule('tests/unit/fixtures/quit-gate-events-entry.ts', {
			viteApiBase: apiOrigin
		});

		const priorWindow = globalThis.window;
		const priorDocument = globalThis.document;
		const shellWindow = {
			OPENDJ_ENGINE_ORIGIN: apiOrigin,
			location: { origin: 'http://127.0.0.1:5273' }
		};
		globalThis.window = shellWindow;
		globalThis.document = {
			addEventListener: () => undefined,
			removeEventListener: () => undefined,
			activeElement: null,
			querySelector: () => null
		};

		try {
			mod.resetShellQuitGateForTests();
			mod.eventsBus._resetForTests();

			let quitCalls = 0;
			mod.ensureEarlyShellQuitGate({
				window: shellWindow,
				detect: () => 'desktop-shell',
				query: () => state(),
				flushSnapshot: () => {},
				exitShell: async () => {
					quitCalls += 1;
				}
			});
			assert.equal(mod.eventsBus.getConnectionState(), 'closed');

			const quitResponse = await fetch(`${apiOrigin}/api/v1/lifecycle/quit`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ force: true })
			});
			assert.equal(quitResponse.status, 200);
			assert.equal(quitCalls, 0, 'quit must not run until the bus delivers shell.quit');

			mod.eventsBus.connect();
			await waitForEventsBusOpen(mod.eventsBus);
			await waitForQuitCalls(() => quitCalls, 1);
		} finally {
			mod.resetShellQuitGateForTests();
			mod.eventsBus._resetForTests();
			mod.eventsBus.disconnect();
			globalThis.window = priorWindow;
			globalThis.document = priorDocument;
		}
	} finally {
		proc?.kill();
	}
});

test('ensureEarlyShellQuitGate uses the real engine WS bus and lifecycle/quit (issue #6262)', async () => {
	let proc;
	let port;
	try {
		({ proc, port } = await startQuitGateTestServer());
		const apiOrigin = `http://127.0.0.1:${port}`;
		const mod = await loadTypeScriptModule('tests/unit/fixtures/quit-gate-events-entry.ts', {
			viteApiBase: apiOrigin
		});

		const priorWindow = globalThis.window;
		const priorDocument = globalThis.document;
		const shellWindow = {
			OPENDJ_ENGINE_ORIGIN: apiOrigin,
			location: { origin: 'http://127.0.0.1:5273' }
		};
		globalThis.window = shellWindow;
		globalThis.document = {
			addEventListener: () => undefined,
			removeEventListener: () => undefined,
			activeElement: null,
			querySelector: () => null
		};

		try {
			mod.resetShellQuitGateForTests();
			mod.eventsBus._resetForTests();

			let quitCalls = 0;
			mod.ensureEarlyShellQuitGate({
				query: () => state(),
				flushSnapshot: () => {},
				exitShell: async () => {
					quitCalls += 1;
				}
			});

			await waitForEventsBusOpen(mod.eventsBus);
			assert.equal(typeof shellWindow.__OPENDJ_requestQuit, 'function');

			const quitResponse = await fetch(`${apiOrigin}/api/v1/lifecycle/quit`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ force: true })
			});
			assert.equal(
				quitResponse.status,
				200,
				'lifecycle/quit must publish shell.quit on the live hub'
			);
			await waitForQuitCalls(() => quitCalls, 1);

			quitCalls = 0;
			shellWindow.__OPENDJ_requestQuit();
			await waitForQuitCalls(() => quitCalls, 1);
		} finally {
			mod.resetShellQuitGateForTests();
			mod.eventsBus._resetForTests();
			mod.eventsBus.disconnect();
			globalThis.window = priorWindow;
			globalThis.document = priorDocument;
		}
	} finally {
		proc?.kill();
	}
});

test('confirmQuit does not reject when exitShell rejects (issue #3058)', async () => {
	await assert.doesNotReject(
		confirmQuit({
			flushSnapshot: () => {},
			exitShell: async () => {
				throw new Error('process:allow-exit not granted');
			}
		})
	);
});
