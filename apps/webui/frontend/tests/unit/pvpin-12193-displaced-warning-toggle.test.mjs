// requirement: PVPIN-12193
// requirement: PVPIN-14832
// PVPIN-12193 (hold ruling on #14721): the displaced-control warning's Turn off
// must not hide it for good. The MIDI panel shows a toggle for it, and the toggle
// (the typed midi_displaced_warning command, so agents and the CLI reach it too)
// turns it back on.
// [if] after Turn off the MIDI panel has no toggle showing the warning off [then] broken, [else stop].
// [if] the toggle's command turns the warning back on and a still-displaced control does not warn again [then] broken, [else stop].
// [if] query() does not report the warning switch [then] broken, [else stop].

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const EQ_HIGH = { type: 'mixer_channel', deck: 1, target: 'eq', band: 'high' };
const KEY = 'opendj.midi.displaced-warning';

let m;
let store;
let hadWindow;

before(async () => {
	hadWindow = 'window' in globalThis;
	store = new Map();
	// Installed before the modules load: the leaf and the policy owner seed from it.
	globalThis.window = {
		localStorage: {
			getItem: (key) => store.get(key) ?? null,
			setItem: (key, value) => void store.set(key, String(value))
		}
	};
	m = await loadSvelteSsrModule(`
		export { render } from 'svelte/server';
		export { default as MidiPanel } from '$lib/components/rb/MidiPanel.svelte';
		export { midiUi } from '$lib/components/rb/midi/midi-ui-state.svelte';
		export {
			observeAbsoluteMidi,
			registerDisplacedWarningSink
		} from '$lib/rb/midi/takeover-state.svelte';
		export * as ipc from '$lib/rb/performance-ipc.svelte';
	`);
});

after(() => {
	if (!hadWindow) delete globalThis.window;
});

/** The MIDI panel's warning checkbox as rendered, or null when there is none. */
function warningToggle() {
	m.midiUi.panelOpen = true;
	const html = m.render(m.MidiPanel, { props: {} }).body;
	const input = html.match(/<input[^>]*data-midi-displaced-warning[^>]*>/);
	return input === null ? null : { checked: /\schecked(\s|=|>|\/)/.test(input[0]) };
}

test('PVPIN-12193: after Turn off, the MIDI panel toggle turns the warning back on and it shows again', async () => {
	const shown = [];
	m.registerDisplacedWarningSink((turnOff) => shown.push(turnOff));
	const uninstall = m.ipc.installPerformanceBrowserIpc();
	const dispatch = (command) => globalThis.window.musicDjToolsPerformance.dispatch(command);
	try {
		// A knob held for pickup far from the software value: a displaced control.
		m.observeAbsoluteMidi(EQ_HIGH, 0.1, 1 / 127, 'flx4-test', 'cc:0:7', 0.9);
		assert.equal(shown.length, 1, 'the warning shows for a displaced control (control)');
		assert.deepEqual(warningToggle(), { checked: true }, 'the panel shows the warning on');

		shown[0](); // the toast's Turn off
		assert.equal(store.get(KEY), 'off');
		assert.equal(m.ipc.queryPerformanceState().midi_takeover.displaced_warning, false);
		assert.deepEqual(warningToggle(), { checked: false }, 'the panel shows the warning off');

		// The toggle dispatches this command: the warning is on again and shows at once.
		const state = await dispatch({ type: 'midi_displaced_warning', enabled: true });
		assert.equal(state.midi_takeover.displaced_warning, true);
		assert.notEqual(store.get(KEY), 'off', 'persisted on');
		assert.equal(shown.length, 2, 'the still-displaced control warns again');
		assert.deepEqual(warningToggle(), { checked: true });

		await assert.rejects(
			dispatch({ type: 'midi_displaced_warning', enabled: 'yes' }),
			/must be a boolean/
		);
	} finally {
		// The knob reaches the software value: no ghost, so no 30 s repeat timer keeps the run alive.
		m.observeAbsoluteMidi(EQ_HIGH, 0.9, 1 / 127, 'flx4-test', 'cc:0:7', 0.9);
		uninstall();
	}
});
