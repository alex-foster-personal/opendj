import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// EL-17-F11: the installed Electron shell carries MIDI on WebMIDI, not on
// Tauri's CoreMIDI snapshot, so the Mixtour Pro's declared master12-cue34
// topology ((private path) AC5) must be applied on the WebMIDI
// path too. Drives the module's real initMidi against a fake MIDIAccess and a
// recorded location.replace; nothing in webmidi.svelte.ts is stubbed.
//
// [if] Electron and a Mixtour Pro is connected at boot [then] the page leaves
//   for ?djio=master12-cue34 exactly once [⛔️ if it stays or replaces twice]
// [if] the Mixtour is plugged in after boot [then] the same redirect fires and
//   the leaving page detaches its handlers [⛔️ if handlers stay attached]
// [if] a plain browser tab, a page already on djio, or a controller with no
//   profile [then] no redirect (controls: a fix that always redirects fails)

const DJIO = 'master12-cue34';
const PAGE = 'http://127.0.0.1:9427/performance';

let webmidi;
let mixtourMap;
let access;
let replaced;

function fakePort(id, name, type = 'input') {
	return { id, name, manufacturer: 'Reloop', state: 'connected', type, onmidimessage: null, send() {} };
}

function fakeAccess() {
	const fake = {
		inputs: new Map(),
		outputs: new Map(),
		onstatechange: null,
		plug(name) {
			const input = fakePort(`in-${name}`, name);
			fake.inputs.set(input.id, input);
			fake.outputs.set(`out-${name}`, fakePort(`out-${name}`, name, 'output'));
			fake.onstatechange?.({ port: input });
			return input;
		}
	};
	return fake;
}

/** A browser window at `search`, optionally hosted by the Electron preload. */
function installWindow(search, { electron }) {
	replaced = [];
	const location = { href: `${PAGE}${search}`, search, replace: (url) => replaced.push(String(url)) };
	globalThis.window = { location };
	if (electron) globalThis.opendjShell = { kind: 'electron', version: 1 };
	else delete globalThis.opendjShell;
}

before(async () => {
	webmidi = await loadTypeScriptModule('src/lib/rb/midi/webmidi.svelte.ts');
	({ RELOOP_MIXTOUR_PRO_MAP: mixtourMap } = await loadTypeScriptModule('src/lib/rb/midi/maps/reloop-mixtour-pro.ts'));
});

beforeEach(() => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(mixtourMap);
	access = fakeAccess();
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		writable: true,
		value: { requestMIDIAccess: async () => access }
	});
});

afterEach(() => {
	webmidi._resetMidiForTests();
	delete globalThis.opendjShell;
	delete globalThis.window;
});

test('EL-17-F11: Electron with a Mixtour Pro at boot leaves for djio exactly once', async () => {
	installWindow('', { electron: true });
	access.inputs.set('in-mx', fakePort('in-mx', 'Reloop Mixtour Pro'));
	await webmidi.initMidi();
	await webmidi.initMidi();
	assert.deepEqual(replaced, [`${PAGE}?djio=${DJIO}`], 'if Electron skips the topology step then master and cue share outputs 1/2 - broken');
});

test('EL-17-F11: a Mixtour plugged in after boot redirects once and the leaving page lets go of WebMIDI', async () => {
	installWindow('', { electron: true });
	await webmidi.initMidi();
	assert.deepEqual(replaced, [], 'nothing connected at boot, nothing to redirect for');
	const input = access.plug('Reloop Mixtour Pro');
	assert.deepEqual(replaced, [`${PAGE}?djio=${DJIO}`]);
	assert.equal(input.onmidimessage, null, 'if the leaving page keeps its handler then presses dispatch on a page being torn down - broken');
	assert.equal(access.onstatechange, null);
	access.inputs.set('in-other', fakePort('in-other', 'Reloop Mixtour Pro'));
	await webmidi.initMidi();
	assert.equal(replaced.length, 1, 'if a later rescan re-issues replace() the reload can loop - broken');
});

test('EL-17-F11 control: a browser tab keeps its page; the redirect is the installed shell contract only', async () => {
	installWindow('', { electron: false });
	access.inputs.set('in-mx', fakePort('in-mx', 'Reloop Mixtour Pro'));
	await webmidi.initMidi();
	assert.deepEqual(replaced, []);
	assert.equal(webmidi.midiState.devices.length, 1, 'the control must still see the controller');
});

test('EL-17-F11 control: a page already on djio, or a controller with no profile, stays put', async () => {
	installWindow('?djio=', { electron: true });
	access.inputs.set('in-mx', fakePort('in-mx', 'Reloop Mixtour Pro'));
	await webmidi.initMidi();
	assert.deepEqual(replaced, [], 'IOPIN-12: an explicit djio is never redirected again');

	webmidi._resetMidiForTests();
	installWindow('', { electron: true });
	access = fakeAccess();
	access.inputs.set('in-ddj', fakePort('in-ddj', 'DDJ-400'));
	await webmidi.initMidi();
	assert.deepEqual(replaced, []);
});
