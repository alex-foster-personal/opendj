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

import { loadRuneModule } from './load-rune-module.mjs';

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
	const stores = await vite.ssrLoadModule('/src/lib/stores.svelte.ts');
	const fmt = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-format.ts');
	// The page's own wiring (startAppInstruments): the failure toast reaches
	// apply.ts through this reporter, since apply.ts does not import stores.
	const appInit = await vite.ssrLoadModule('/src/lib/rb/app-init.ts');
	apply.setMidiLoadFailureReporter(appInit.toastMidiLoadFailure);
	const loadFailureToasts = () =>
		stores.toasts.filter((t) => t.message === 'MIDI could not load, so it stays off').length;

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

	/** The MIDI entry's label as HeadphoneCluster derives it, every input read
	 * from the real modules (no device is mapped in Node). */
	const liveLabel = () => {
		const on = choice.midiEnabledPersisted();
		const { permission } = webmidi.midiState;
		const { requestPending } = uiState.midiUi;
		return {
			on,
			pending: requestPending,
			status: fmt.midiLabelStatus(permission, requestPending, false, on),
			title: fmt.midiLabelTitle(permission, requestPending, 0, webmidi.midiState.devices.length, on)
		};
	};

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
	// Control for the failed-load path in 7: here the runtime chunk loads for
	// real, so the choice is cleared by the runtime's own unsupported branch
	// (lastError), never by apply.ts's load-failure catch.
	results.enableFromSettings = {
		...snapshot(),
		settingRightAfterEnable,
		loadFailureToasts: loadFailureToasts()
	};

	// 3. Turned off while the request is pending (the same function apply.ts
	// calls, invoked directly so the disable lands inside the pending window).
	localStorage.clear();
	const enabling = uiState.applyMidiEnabledSetting(true);
	const pendingAtDisable = uiState.midiUi.requestPending;
	const labelPendingOn = liveLabel();
	// Not awaited yet: the off lands while the prompt is still pending (Codex
	// P2 4131292220), and the label is read inside that window.
	const disabling = uiState.applyMidiEnabledSetting(false);
	const labelPendingOff = liveLabel();
	await disabling;
	await enabling;
	results.disableWhilePending = { ...snapshot(), pendingAtDisable, labelPendingOn, labelPendingOff };

	// 4. Off and on again while the first request is still pending.
	localStorage.clear();
	const first = uiState.applyMidiEnabledSetting(true);
	const pendingAtToggle = uiState.midiUi.requestPending;
	const off = uiState.applyMidiEnabledSetting(false);
	const again = uiState.applyMidiEnabledSetting(true);
	await Promise.all([first, off, again]);
	results.reenableWhilePending = { ...snapshot(), pendingAtToggle };

	// 5. CTRL-06: a persisted opt-in survives a boot that has no MIDI to ask for.
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

	// 9. The I/O MIDI entry's color after the user turns MIDI off (Codex P2,
	// PR #3896). HeadphoneCluster feeds midiEnabledPersisted() into the real
	// derivation below. 'granted' is the one input no headless host can
	// produce (header of the parent test): it is supplied, while the choice
	// and the emptied device list come from the real store and the real
	// settings path. An opt-in with nothing mapped is a lost device (red);
	// the same state after settings turns MIDI off is gray, not a fault.
	const label = () => {
		const on = choice.midiEnabledPersisted();
		const n = webmidi.midiState.devices.length;
		return {
			on,
			pending: uiState.midiUi.requestPending,
			status: fmt.midiLabelStatus('granted', uiState.midiUi.requestPending, false, on),
			title: fmt.midiLabelTitle('granted', uiState.midiUi.requestPending, 0, n, on)
		};
	};
	localStorage.clear();
	localStorage.setItem(choice.MIDI_ENABLED_KEY, '1');
	const optedIn = label();
	apply.applySettingChange('rb.midi_enabled', false);
	await until(() => !choice.midiEnabledPersisted(), 'the off choice to persist');
	await new Promise((r) => setTimeout(r, 50));
	results.labelAfterOff = { optedIn, turnedOff: label(), devices: webmidi.midiState.devices.length };
} finally {
	await vite.close();
}

// 7. The settings toggle's displayed value must re-render: a live $effect
// (runes compiled for the client by load-rune-module.mjs, not stubbed) reads
// readSettingValue() the way SettingsOverlay's template does. That bundle
// keeps apply.ts's dynamic import of midi-ui-state EXTERNAL, and Node's real
// loader cannot resolve a `$lib/...` specifier, so the MIDI runtime chunk
// genuinely fails to load here (Codex P2 4129637645). The restore to off and
// the error toast below are that real failure's outcome, not a simulated one.
localStorage.clear();
const rune = await loadRuneModule(`
import { applySettingChange, readSettingValue, setMidiLoadFailureReporter } from '$lib/settings/apply';
import { toastMidiLoadFailure } from '$lib/rb/app-init';
import { hydrateMidiEnabledFromDisk, midiEnabledPersisted } from '$lib/components/rb/midi/midi-enabled-choice';
import { dismissToast, toasts } from '$lib/stores.svelte';
export function watchMidiSetting() {
	const seen: unknown[] = [];
	const stop = $effect.root(() => {
		$effect(() => {
			seen.push(readSettingValue('rb.midi_enabled'));
		});
	});
	return { seen, stop };
}
export function errorToasts() {
	return toasts.filter((t) => t.kind === 'error').map((t) => t.message);
}
export function dismissAll() {
	for (const t of [...toasts]) dismissToast(t.logId);
}
export { applySettingChange, hydrateMidiEnabledFromDisk, midiEnabledPersisted, setMidiLoadFailureReporter, toastMidiLoadFailure };
`);
// As startAppInstruments wires it on every page.
rune.setMidiLoadFailureReporter(rune.toastMidiLoadFailure);
const settle = () => new Promise((r) => setTimeout(r, 20));
const watch = rune.watchMidiSetting();
await settle();
rune.applySettingChange('rb.midi_enabled', true); // the user's toggle
const persistedRightAfterToggle = rune.midiEnabledPersisted();
for (let i = 0; i < 400 && rune.midiEnabledPersisted(); i += 1) await settle();
const persistedAfterFailedLoad = rune.midiEnabledPersisted();
const toastsAfterFailedLoad = rune.errorToasts();
rune.dismissAll(); // each toast arms a real dismissal timer
await settle();
rune.hydrateMidiEnabledFromDisk({ midi_enabled: true }); // disk choice arriving later
await settle();
watch.stop();
results.displayReactivity = { seen: watch.seen };
results.failedRuntimeLoad = { persistedRightAfterToggle, persistedAfterFailedLoad, toastsAfterFailedLoad };

// 8. Control: with the reporter unwired (app-init's teardown), the same real
// failure still restores off but raises no toast, so the toast above came
// through the reporter and nowhere else.
rune.setMidiLoadFailureReporter(null);
rune.applySettingChange('rb.midi_enabled', true);
const unwiredPersistedRightAfterToggle = rune.midiEnabledPersisted();
for (let i = 0; i < 400 && rune.midiEnabledPersisted(); i += 1) await settle();
results.unwiredFailedRuntimeLoad = {
	persistedRightAfterToggle: unwiredPersistedRightAfterToggle,
	persistedAfterFailedLoad: rune.midiEnabledPersisted(),
	toastsAfterFailedLoad: rune.errorToasts()
};
rune.dismissAll();

process.stdout.write(`RESULT ${JSON.stringify(results)}\n`);
