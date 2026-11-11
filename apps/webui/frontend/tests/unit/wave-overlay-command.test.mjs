// requirement: WAVEQS-11736
// [if] set_waveform_overlay grid_lines does not persist and read back each value, or accepts a bad shape [then] this fails, [else stop].
// requirement: WAVEQS-8348e
// [if] set_waveform_overlay does not reach the same persisted pref the Settings row writes, for each overlay [then] this fails, [else stop].
// [if] the performance snapshot does not read back the overlay the command just set [then] this fails, [else stop].
// [if] set_waveform_overlay accepts an unknown overlay or a non-boolean enabled [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://wave-overlay.example.test';

let ipc;
let overlay;

/** The ui-prefs key each overlay must write: the Settings row it twins. */
const OVERLAY_PREF = {
	sections: 'show_sections',
	lyrics: 'lyrics_waveform_overlay',
	lyrics_scrub: 'lyrics_hover_scrub',
	jog_radial: 'jog_radial_waveform',
	vocal_lane: 'show_vocal_lane'
};

before(async () => {
	overlay = await loadTypeScriptModule('src/lib/rb/waveform-overlay-command.ts');
	await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: API_BASE
	});
});

function installPrefsStorage() {
	const store = new Map();
	const storage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, v),
		removeItem: (k) => store.delete(k)
	};
	const original = globalThis.localStorage;
	globalThis.localStorage = storage;
	return {
		storage,
		saved: () => JSON.parse(store.get('mdt.rb.ui-prefs.v1') ?? '{}'),
		restore() {
			if (original === undefined) delete globalThis.localStorage;
			else globalThis.localStorage = original;
		}
	};
}

test('set_waveform_overlay writes each overlay to the pref its Settings row owns, both ways', async () => {
	const prefs = installPrefsStorage();
	globalThis.window = { localStorage: prefs.storage };
	const uninstall = ipc.installPerformanceBrowserIpc();
	const dispatch = (c) => window.musicDjToolsPerformance.dispatch(c);
	try {
		assert.deepEqual([...overlay.WAVEFORM_BOOL_OVERLAYS].sort(), Object.keys(OVERLAY_PREF).sort());
		for (const name of overlay.WAVEFORM_BOOL_OVERLAYS) {
			const key = OVERLAY_PREF[name];
			await dispatch({ type: 'set_waveform_overlay', overlay: name, enabled: true });
			assert.equal(prefs.saved()[key], true, `${name} on did not persist ${key}`);
			assert.equal(
				ipc.queryPerformanceState().ui.waveform_overlays[name],
				true,
				`${name} on is not in the snapshot`
			);
			await dispatch({ type: 'set_waveform_overlay', overlay: name, enabled: false });
			assert.equal(prefs.saved()[key], false, `${name} off did not persist ${key}`);
			assert.equal(
				ipc.queryPerformanceState().ui.waveform_overlays[name],
				false,
				`${name} off is not in the snapshot`
			);
		}
		await assert.rejects(
			dispatch({ type: 'set_waveform_overlay', overlay: 'beats', enabled: true }),
			/overlay must be/
		);
		await assert.rejects(
			dispatch({ type: 'set_waveform_overlay', overlay: 'sections', enabled: 'yes' }),
			/enabled/
		);
		await assert.rejects(
			dispatch({ type: 'set_waveform_overlay', overlay: 'sections', enabled: true, extra: 1 }),
			/extra|unexpected|key/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
		prefs.restore();
	}
});

test('WAVEQS-11736 set_waveform_overlay grid_lines persists each value and the snapshot reads it back', async () => {
	const prefs = installPrefsStorage();
	globalThis.window = { localStorage: prefs.storage };
	const uninstall = ipc.installPerformanceBrowserIpc();
	const dispatch = (c) => window.musicDjToolsPerformance.dispatch(c);
	try {
		for (const value of ['bars', 'off', 'beats_bars']) {
			await dispatch({ type: 'set_waveform_overlay', overlay: 'grid_lines', value });
			assert.equal(prefs.saved().wave_grid_lines, value, `${value} did not persist`);
			assert.equal(
				ipc.queryPerformanceState().ui.wave_grid_lines,
				value,
				`${value} is not in the snapshot`
			);
		}
		await assert.rejects(
			dispatch({ type: 'set_waveform_overlay', overlay: 'grid_lines', value: 'beats' }),
			/value must be/
		);
		await assert.rejects(
			dispatch({ type: 'set_waveform_overlay', overlay: 'grid_lines', enabled: true }),
			/enabled|unexpected|key|value/i
		);
		await assert.rejects(
			dispatch({ type: 'set_waveform_overlay', overlay: 'sections', value: 'bars' }),
			/value|unexpected|key/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
		prefs.restore();
	}
});
