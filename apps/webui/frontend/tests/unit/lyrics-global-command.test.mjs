// requirement: LYR-14854
// [if] set_lyrics_global enabled true or false does not persist lyrics_global and read it back in the snapshot [then] this fails, [else stop].
// [if] set_lyrics_global writes any other lyric pref, or repeating it flips the switch [then] this fails, [else stop].
// [if] set_lyrics_global with a missing, non-boolean or extra field changes the prefs or saved bytes [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://lyrics-global.example.test';
const PREFS_KEY = 'mdt.rb.ui-prefs.v1';

let ipc;

before(async () => {
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
	globalThis.window = { localStorage: storage };
	return {
		bytes: () => store.get(PREFS_KEY) ?? null,
		saved: () => JSON.parse(store.get(PREFS_KEY) ?? '{}'),
		restore() {
			delete globalThis.window;
			if (original === undefined) delete globalThis.localStorage;
			else globalThis.localStorage = original;
		}
	};
}

/** Runs `body` with the page bus installed over a fresh prefs store. */
async function withBus(body) {
	const prefs = installPrefsStorage();
	const uninstall = ipc.installPerformanceBrowserIpc();
	const dispatch = (c) => window.musicDjToolsPerformance.dispatch(c);
	try {
		await body(dispatch, prefs);
	} finally {
		uninstall();
		prefs.restore();
	}
}

test('set_lyrics_global persists the switch both ways and the snapshot reads it back', async () => {
	await withBus(async (dispatch, prefs) => {
		for (const enabled of [false, true, false, true]) {
			await dispatch({ type: 'set_lyrics_global', enabled });
			assert.equal(prefs.saved().lyrics_global, enabled, `enabled ${enabled} did not persist`);
			assert.equal(ipc.queryPerformanceState().ui.lyrics_global, enabled);
		}
	});
});

test('set_lyrics_global is idempotent and leaves the other lyric prefs alone', async () => {
	await withBus(async (dispatch, prefs) => {
		await dispatch({ type: 'set_waveform_overlay', overlay: 'lyrics', enabled: true });
		await dispatch({ type: 'set_waveform_overlay', overlay: 'lyrics_scrub', enabled: false });
		await dispatch({ type: 'set_lyrics_global', enabled: false });
		await dispatch({ type: 'set_lyrics_global', enabled: false });
		const ui = ipc.queryPerformanceState().ui;
		assert.equal(ui.lyrics_global, false, 'a repeated off flipped the switch back');
		assert.equal(ui.waveform_overlays.lyrics, true, 'the global switch wrote the waveform overlay');
		assert.equal(ui.waveform_overlays.lyrics_scrub, false, 'the global switch wrote lyrics_scrub');
		assert.equal(prefs.saved().lyrics_waveform_overlay, true);
		await dispatch({ type: 'set_lyrics_global', enabled: true });
		assert.equal(ipc.queryPerformanceState().ui.waveform_overlays.lyrics_scrub, false);
	});
});

test('set_lyrics_global refuses a bad shape by field name and mutates nothing', async () => {
	await withBus(async (dispatch, prefs) => {
		await dispatch({ type: 'set_lyrics_global', enabled: true });
		const before = prefs.bytes();
		const cases = [
			[{ type: 'set_lyrics_global' }, /enabled must be boolean/],
			[{ type: 'set_lyrics_global', enabled: 'false' }, /enabled must be boolean/],
			[{ type: 'set_lyrics_global', enabled: 0 }, /enabled must be boolean/],
			[{ type: 'set_lyrics_global', enabled: false, deck: 1 }, /unexpected fields: deck/],
			[
				{ type: 'set_lyrics_global', enabled: false, overlay: 'lyrics', extra: 1 },
				/unexpected fields: overlay, extra/
			]
		];
		for (const [command, message] of cases) {
			await assert.rejects(dispatch(command), message, JSON.stringify(command));
			assert.equal(prefs.bytes(), before, `${JSON.stringify(command)} changed the saved prefs`);
			assert.equal(ipc.queryPerformanceState().ui.lyrics_global, true);
		}
	});
});

test('set_lyrics_global has no deck queue scope', () => {
	assert.equal(
		ipc.performanceCommandQueueScopes({ type: 'set_lyrics_global', enabled: true }),
		null
	);
});
