/**
 * requirement: CHROME-07
 *
 * The rb.midi_enabled setting must act on the live MIDI runtime, not only
 * persist a flag (Codex P1 on PR #3896). Enabling from settings requests
 * WebMIDI access through the same path as the page-load auto-enable;
 * disabling detaches the action glue and every input listener without
 * faking the browser's permission state.
 *
 * Real MIDIAccess objects and hardware are not available in Node, so this
 * drives settings/apply.ts's real entry point (applySettingChange) against a
 * fake navigator.requestMIDIAccess whose port maps are plain objects - the
 * same harness midi-hotplug.test.mjs uses. Nothing in the modules under test
 * is stubbed.
 *
 * Regression lines:
 * - if enabling from settings does not call requestMIDIAccess then the toggle
 *   reads "on" while no controller works until a reload
 * - if disabling from settings leaves onmidimessage attached then a controller
 *   keeps driving the decks after the user turned MIDI off
 * - if disabling rewrites midiState.permission then the panel lies about what
 *   the browser granted
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let apply;
let uiState;
let webmidi;
let choice;
let access;
let requestCalls;

function fakeInput(id, name) {
	return { id, name, manufacturer: 'Pioneer DJ', state: 'connected', type: 'input', onmidimessage: null };
}

function fakeAccess(inputs) {
	return { inputs: new Map(inputs.map((i) => [i.id, i])), outputs: new Map(), onstatechange: null };
}

function installLocalStorage(initial = {}) {
	const store = new Map(Object.entries(initial));
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v)),
		removeItem: (k) => store.delete(k),
		clear: () => store.clear()
	};
}

/** applySettingChange fires the runtime half without awaiting it (the
 * module loads on demand), so wait on the observable state it must reach. */
async function until(predicate, what) {
	for (let i = 0; i < 200; i += 1) {
		if (predicate()) return;
		await new Promise((r) => setTimeout(r, 5));
	}
	assert.fail(`timed out waiting for ${what}`);
}

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	apply = await vite.ssrLoadModule('/src/lib/settings/apply.ts');
	uiState = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-ui-state.svelte.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	choice = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-enabled-choice.ts');
});

after(async () => {
	uiState.disableMidi();
	delete globalThis.localStorage;
	await vite.close();
});

beforeEach(() => {
	uiState.disableMidi();
	uiState.midiUi.lastError = null;
	installLocalStorage();
	access = fakeAccess([fakeInput('in-1', 'DDJ-FLX10 MIDI 1')]);
	requestCalls = 0;
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		writable: true,
		value: {
			requestMIDIAccess: async () => {
				requestCalls += 1;
				return access;
			}
		}
	});
});

test('enabling rb.midi_enabled from settings requests WebMIDI access and attaches input listeners', async () => {
	const input = access.inputs.get('in-1');
	apply.applySettingChange('rb.midi_enabled', true);
	assert.equal(apply.readSettingValue('rb.midi_enabled'), true, 'the choice lands synchronously');
	await until(() => requestCalls === 1 && !uiState.midiUi.requestPending, 'the access request');
	assert.equal(uiState.midiUi.lastError, null);
	assert.equal(webmidi.midiState.permission, 'granted');
	assert.deepEqual(webmidi.midiState.devices.map((d) => d.id), ['in-1']);
	assert.equal(typeof input.onmidimessage, 'function', 'the input must be listened to');
	assert.equal(typeof access.onstatechange, 'function', 'hot-plug must be wired');
	assert.equal(webmidi._actionHandlerRegisteredForTests(), true, 'the action glue must be attached');
	assert.equal(choice.midiEnabledPersisted(), true);
});

test('disabling rb.midi_enabled from settings detaches the glue and input listeners, leaving permission as granted', async () => {
	const input = access.inputs.get('in-1');
	apply.applySettingChange('rb.midi_enabled', true);
	await until(() => typeof input.onmidimessage === 'function', 'MIDI to come up');

	apply.applySettingChange('rb.midi_enabled', false);
	assert.equal(apply.readSettingValue('rb.midi_enabled'), false, 'the choice lands synchronously');
	await until(() => input.onmidimessage === null, 'the input listener to detach');
	assert.equal(access.onstatechange, null, 'hot-plug listener must detach too');
	assert.equal(webmidi._actionHandlerRegisteredForTests(), false, 'the action glue must detach');
	assert.deepEqual(webmidi.midiState.devices, []);
	assert.equal(webmidi.midiState.permission, 'granted', 'the browser grant is not ours to rewrite');
	assert.equal(choice.midiEnabledPersisted(), false);
	assert.equal(requestCalls, 1, 'disable must not request access');
});

test('re-enabling after a settings disable re-requests access and re-arms the glue', async () => {
	const input = access.inputs.get('in-1');
	apply.applySettingChange('rb.midi_enabled', true);
	await until(() => typeof input.onmidimessage === 'function', 'MIDI to come up');
	apply.applySettingChange('rb.midi_enabled', false);
	await until(() => input.onmidimessage === null, 'the input listener to detach');

	apply.applySettingChange('rb.midi_enabled', true);
	await until(() => requestCalls === 2 && typeof input.onmidimessage === 'function', 'MIDI to come back');
	assert.equal(webmidi._actionHandlerRegisteredForTests(), true);
});

test('page-load auto-enable still requests access for a persisted opt-in (control)', async () => {
	installLocalStorage({ [choice.MIDI_ENABLED_KEY]: '1' });
	const input = access.inputs.get('in-1');
	await uiState.maybeAutoEnableMidi();
	assert.equal(requestCalls, 1);
	assert.equal(uiState.midiUi.lastError, null);
	assert.equal(typeof input.onmidimessage, 'function');
	assert.equal(webmidi._actionHandlerRegisteredForTests(), true);
});

test('page-load auto-enable stays idle without an opt-in (control)', async () => {
	await uiState.maybeAutoEnableMidi();
	assert.equal(requestCalls, 0);
	assert.equal(access.inputs.get('in-1').onmidimessage, null);
});
