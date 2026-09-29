// Child process for midi-enabled-setting-runtime.test.mjs. Not a test file
// (the unit glob is *.test.mjs); the parent spawns it with Node's REAL Web
// Storage (--experimental-webstorage --localstorage-file=<fresh file>) so the
// persisted MIDI choice is read and written through a genuine localStorage,
// not a stand-in. Node has no WebMIDI, so every access request here meets the
// real "unsupported" branch of initMidi(). Nothing is stubbed or patched.
//
// Prints one line, `RESULT <json>`, mapping each scenario to what the real
// modules reported.
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

if (typeof localStorage === 'undefined') {
	throw new Error('child must run with --experimental-webstorage --localstorage-file=<path>');
}

const vite = await createServer({
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

const results = {};
try {
	const apply = await vite.ssrLoadModule('/src/lib/settings/apply.ts');
	const uiState = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-ui-state.svelte.ts');
	const webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	const choice = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-enabled-choice.ts');

	const snapshot = () => ({
		navigatorHasWebMidi: typeof navigator !== 'undefined' && navigator.requestMIDIAccess !== undefined,
		permission: webmidi.midiState.permission,
		devices: webmidi.midiState.devices.length,
		lastError: uiState.midiUi.lastError,
		requestPending: uiState.midiUi.requestPending,
		persisted: choice.midiEnabledPersisted(),
		setting: apply.readSettingValue('rb.midi_enabled'),
		glueAttached: webmidi._actionHandlerRegisteredForTests()
	});

	/** applySettingChange fires its runtime half without awaiting it (the
	 * module loads on demand), so wait for the state it must reach. */
	async function until(predicate, what) {
		for (let i = 0; i < 400; i += 1) {
			if (predicate()) return;
			await new Promise((r) => setTimeout(r, 5));
		}
		throw new Error(`timed out waiting for ${what}`);
	}

	// 1. Control, first so nothing before it has touched the request path.
	await uiState.maybeAutoEnableMidi();
	results.idleAutoEnable = snapshot();

	// 2. The settings entry point itself.
	localStorage.clear();
	apply.applySettingChange('rb.midi_enabled', true);
	const settingRightAfterEnable = apply.readSettingValue('rb.midi_enabled');
	await until(() => uiState.midiUi.lastError !== null && !uiState.midiUi.requestPending, 'the request to settle');
	results.enableFromSettings = { ...snapshot(), settingRightAfterEnable };

	// 3. Turned off while the request is pending (the same function apply.ts
	// calls, invoked directly so the disable lands inside the pending window).
	localStorage.clear();
	const enabling = uiState.applyMidiEnabledSetting(true);
	const pendingAtDisable = uiState.midiUi.requestPending;
	await uiState.applyMidiEnabledSetting(false);
	await enabling;
	results.disableWhilePending = { ...snapshot(), pendingAtDisable };

	// 4. Off and on again while the first request is still pending.
	localStorage.clear();
	const first = uiState.applyMidiEnabledSetting(true);
	const pendingAtToggle = uiState.midiUi.requestPending;
	const off = uiState.applyMidiEnabledSetting(false);
	const again = uiState.applyMidiEnabledSetting(true);
	await Promise.all([first, off, again]);
	results.reenableWhilePending = { ...snapshot(), pendingAtToggle };

	// 5. Control: a persisted opt-in still drives the page-load auto-enable.
	localStorage.clear();
	localStorage.setItem(choice.MIDI_ENABLED_KEY, '1');
	const persistedBeforeAuto = choice.midiEnabledPersisted();
	await uiState.maybeAutoEnableMidi();
	results.autoEnableOptedIn = { ...snapshot(), persistedBeforeAuto };

	// 6. Disable from settings after a failed enable.
	apply.applySettingChange('rb.midi_enabled', false);
	const settingRightAfterDisable = apply.readSettingValue('rb.midi_enabled');
	await new Promise((r) => setTimeout(r, 50));
	results.disableFromSettings = { ...snapshot(), settingRightAfterDisable };
} finally {
	await vite.close();
}

process.stdout.write(`RESULT ${JSON.stringify(results)}\n`);
