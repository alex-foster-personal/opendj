// Issue #10076: first-run MIDI controller setup prompt on /performance.
//
// Regression lines:
//   if a replugged unmapped port prompts twice in one session then broken
//   if Not now does not persist the port to midi_setup_dismissed_ports then broken
//   if Set up does not open the panel with setupPortName set then broken

import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let uiState;
let prefs;
let putBodies = [];
let originalFetch;

function _installLocalStorage(initial = {}) {
	const store = new Map(Object.entries(initial));
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v)),
		removeItem: (k) => store.delete(k),
		clear: () => store.clear()
	};
	return store;
}

function _uninstallLocalStorage() {
	delete globalThis.localStorage;
}

function _stubFetch() {
	originalFetch = globalThis.fetch;
	globalThis.fetch = async (input, init) => {
		const url = typeof input === 'string' ? input : input.url;
		const method = init?.method ?? (typeof input === 'object' ? input.method : 'GET');
		if (String(url).includes('/api/v1/ui-prefs') && method === 'PUT') {
			const raw = init?.body ?? (typeof input === 'object' ? await input.clone().text() : '');
			const body = typeof raw === 'string' ? JSON.parse(raw) : await raw.json?.();
			putBodies.push(body);
			return new Response(JSON.stringify(body), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
}

function _restoreFetch() {
	globalThis.fetch = originalFetch;
}

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	_installLocalStorage();
	_stubFetch();
	uiState = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-ui-state.svelte.ts');
	prefs = await vite.ssrLoadModule('/src/lib/rb/prefs.svelte.ts');
});

after(async () => {
	_restoreFetch();
	_uninstallLocalStorage();
	await vite?.close();
});

beforeEach(() => {
	uiState._resetFirstRunPromptForTests();
	prefs.uiPrefs.midi_setup_dismissed_ports = [];
	putBodies = [];
});

const ACME = { name: 'Acme Pad', mapVendor: null };

function note(devices) {
	uiState.noteMidiDevicesForFirstRun(devices, prefs.uiPrefs.midi_setup_dismissed_ports);
}

function collectPrompt() {
	const p = uiState.midiUi.firstRunPromptPort;
	return p;
}

test('one prompt across device re-enumeration in the same session', () => {
	const seen = [];
	const record = () => {
		const p = collectPrompt();
		if (p !== null && !seen.includes(p)) seen.push(p);
	};
	note([ACME]);
	record();
	note([]);
	record();
	note([{ ...ACME, id: 'new-id' }]);
	record();
	note([ACME]);
	record();
	assert.deepEqual(seen, ['Acme Pad']);
	uiState.dismissFirstRunPrompt();
	note([ACME]);
	assert.equal(uiState.midiUi.firstRunPromptPort, null);
});

test('a mapped device never prompts', () => {
	note([{ name: 'DDJ-FLX4', mapVendor: 'Pioneer DJ' }]);
	assert.equal(uiState.midiUi.firstRunPromptPort, null);
});

test('a dismissed port never prompts', () => {
	prefs.uiPrefs.midi_setup_dismissed_ports = ['Acme Pad'];
	note([ACME]);
	assert.equal(uiState.midiUi.firstRunPromptPort, null);
});

test('Not now persists the port in prefs', () => {
	note([ACME]);
	assert.equal(uiState.midiUi.firstRunPromptPort, 'Acme Pad');
	uiState.dismissFirstRunPrompt();
	assert.ok(prefs.uiPrefs.midi_setup_dismissed_ports.includes('Acme Pad'));
	assert.throws(() => uiState.dismissFirstRunPrompt(), /no prompt/);
});

test('Set up opens the panel and completion adds the port to prefs', () => {
	note([ACME]);
	assert.equal(uiState.midiUi.firstRunPromptPort, 'Acme Pad');
	uiState.startFirstRunSetup();
	assert.equal(uiState.midiUi.setupPortName, 'Acme Pad');
	assert.equal(uiState.midiUi.panelOpen, true);
	assert.ok(!prefs.uiPrefs.midi_setup_dismissed_ports.includes('Acme Pad'));
	note([{ name: 'Acme Pad', mapVendor: 'Acme' }]);
	assert.ok(prefs.uiPrefs.midi_setup_dismissed_ports.includes('Acme Pad'));
	assert.equal(uiState.midiUi.setupPortName, null);
});

test('completeControllerSetup adds the port directly', () => {
	uiState.completeControllerSetup('Port X');
	assert.deepEqual(prefs.uiPrefs.midi_setup_dismissed_ports, ['Port X']);
});

test('two unmapped devices queue: first prompt then second after Not now', () => {
	note([
		{ name: 'Device A', mapVendor: null },
		{ name: 'Device B', mapVendor: null }
	]);
	assert.equal(uiState.midiUi.firstRunPromptPort, 'Device A');
	uiState.dismissFirstRunPrompt();
	assert.equal(uiState.midiUi.firstRunPromptPort, 'Device B');
	uiState.dismissFirstRunPrompt();
	assert.equal(uiState.midiUi.firstRunPromptPort, null);
	const dismissed = prefs.uiPrefs.midi_setup_dismissed_ports;
	assert.deepEqual(dismissed.sort(), ['Device A', 'Device B']);
});

test('SSR render shows port and actions without dialog semantics', async () => {
	const ENTRY = [
		"export { default as Prompt } from '$lib/components/rb/midi/MidiFirstRunPrompt.svelte';",
		"export { midiUi } from '$lib/components/rb/midi/midi-ui-state.svelte';",
		"export { render } from 'svelte/server';"
	].join('\n');
	const mod = await loadSvelteSsrModule(ENTRY);
	mod.midiUi.firstRunPromptPort = 'Test Controller';
	const { body } = mod.render(mod.Prompt);
	assert.match(body, /Test Controller/);
	assert.match(body, /Set up/);
	assert.match(body, /Not now/);
	assert.doesNotMatch(body, /aria-modal/);
	assert.doesNotMatch(body, /role="dialog"/);
	mod.midiUi.firstRunPromptPort = null;
});
