// The MIDI engine loads on demand (PR #3837 bundle budget): the takeover
// policy, the WebMIDI transport, the action glue and the device maps are not
// /performance or library first-paint weight. These tests pin the two
// inverted edges that made that possible, plus the static import boundaries,
// so a later "just import it" cannot quietly put the engine back on boot.
//
// Regression lines:
//   if a takeover mode set before the policy loads is not the policy's initial mode then broken
//   if setMidiTakeoverMode after the policy loads does not reach the policy then broken
//   if a controller reconnect through webmidi does not rearm takeover pickup then broken
//   if a first-paint module statically imports the MIDI engine again then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const m = await loadTypeScriptModule('tests/unit/fixtures/midi-takeover-lazy-entry.ts');

const trim = (deck) => ({ type: 'mixer_channel', deck, target: 'trim' });

/** One fake WebMIDI input on a fake MIDIAccess (see midi-hotplug.test.mjs). */
function fakeAccessWith(input) {
	return { inputs: new Map([[input.id, input]]), outputs: new Map(), onstatechange: null };
}

test('a takeover mode set before the policy loads seeds the policy, and later sets reach it', async () => {
	m.ui.setMidiTakeoverMode('jump');
	const takeover = await m.loadTakeoverState();

	// Jump applies a first observation far from the software value; pickup holds it.
	const seeded = takeover.observeAbsoluteMidi(trim(1), 0.1, 1 / 127, 'seed-dev', 'cc:1:11', 0.8);
	assert.equal(seeded?.apply, true, 'the policy did not start in the mode chosen before it loaded');

	m.ui.setMidiTakeoverMode('pickup');
	assert.equal(m.ui.midiTakeoverUi.mode, 'pickup');
	const held = takeover.observeAbsoluteMidi(trim(2), 0.1, 1 / 127, 'seed-dev', 'cc:1:12', 0.8);
	assert.equal(held?.apply, false, 'a mode change after load never reached the policy');
	assert.deepEqual(m.ui.midiTakeoverGhost('mixer:2:trim'), { value: 0.1, target: 0.8 });
});

test('a controller reconnect through webmidi rearms takeover pickup', async () => {
	const takeover = await m.loadTakeoverState();
	m.ui.setMidiTakeoverMode('pickup');
	const deviceId = 'in-rearm';
	const control = 'cc:1:13';

	// Picked up at 0.5, then the hardware moves freely: not armed, so it applies.
	assert.equal(takeover.observeAbsoluteMidi(trim(3), 0.5, 1 / 127, deviceId, control, 0.5)?.apply, true);
	assert.equal(takeover.observeAbsoluteMidi(trim(3), 0.3, 1 / 127, deviceId, control, 0.5)?.apply, true);

	// The same port (re)appears: webmidi's rescan must rearm that device.
	m.webmidi._resetMidiForTests();
	const input = { id: deviceId, name: 'Unmapped Pad', manufacturer: 'x', state: 'connected', onmidimessage: null };
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		writable: true,
		value: { requestMIDIAccess: async () => fakeAccessWith(input) }
	});
	await m.webmidi.initMidi();
	assert.equal(m.webmidi.midiState.devices.length, 1);

	const afterReconnect = takeover.observeAbsoluteMidi(trim(3), 0.1, 1 / 127, deviceId, control, 0.5);
	assert.equal(afterReconnect?.apply, false, 'the reconnect did not rearm pickup for that device');
	m.webmidi._resetMidiForTests();
});

test('first-paint modules reach the MIDI engine only through dynamic imports', () => {
	const src = (path) => readFileSync(new URL(`../../src/lib/${path}`, import.meta.url), 'utf8');
	const staticImportOf = (text, target) =>
		new RegExp(String.raw`^\s*import\s[^;]*from\s+'[^']*${target}'`, 'm').test(text);

	const uiState = src('components/rb/midi/midi-ui-state.svelte.ts');
	for (const engine of ['midi/midi-engine', 'midi/webmidi.svelte', 'midi/maps', 'midi/action-glue.svelte', 'midi/installed-maps']) {
		assert.equal(staticImportOf(uiState, engine), false, `midi-ui-state statically imports ${engine}`);
	}
	assert.match(uiState, /import\('\$lib\/rb\/midi\/midi-engine'\)/, 'requestMidiAccess no longer loads the engine entry');
	assert.equal(staticImportOf(src('rb/midi/webmidi.svelte.ts'), 'takeover-state.svelte'), false);
	assert.equal(staticImportOf(src('rb/performance-ipc.svelte.ts'), 'takeover-state.svelte'), false);
	assert.equal(staticImportOf(src('components/rb/mixer/HeadphoneCluster.svelte'), 'webmidi.svelte'), false);
	const topBar = src('components/rb/TopBar.svelte');
	assert.equal(staticImportOf(topBar, 'MidiPanel.svelte'), false, 'TopBar mounts MidiPanel statically');
	assert.equal(staticImportOf(topBar, 'MidiLearnLogPopout.svelte'), false);
	for (const path of ['api.ts', 'rb/api-rb.ts', 'rb/audio-engine.svelte.ts']) {
		assert.equal(staticImportOf(src(path), 'stick-session-edits'), false, `${path} statically imports stick-session-edits`);
	}
});

test('the MIDI drawer loader caches one import and words a failed load for the alert', async () => {
	const loader = await loadTypeScriptModule('src/lib/components/rb/midi/midi-panel-loader.ts');
	const first = loader.loadMidiPanel();
	assert.equal(loader.loadMidiPanel(), first, 'a second open re-imported the drawer chunk');
	assert.equal(typeof (await first).default, 'function');
	assert.equal(
		loader.midiSurfaceLoadFailure('MIDI panel', new Error('Failed to fetch dynamically imported module')),
		'MIDI panel failed to load: Failed to fetch dynamically imported module. Reload the page to retry.'
	);
});
