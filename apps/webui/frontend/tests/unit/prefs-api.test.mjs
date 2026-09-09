import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://prefs-api.example.test';

let prefs;
let originalFetch;

function installPrefsStorage(raw = null) {
	const values = new Map(raw === null ? [] : [['mdt.rb.ui-prefs.v1', raw]]);
	const originalWindow = globalThis.window;
	globalThis.window = {
		localStorage: {
			getItem: (key) => values.get(key) ?? null,
			setItem: (key, value) => values.set(key, value)
		}
	};
	return {
		values,
		restore: () => {
			if (originalWindow === undefined) delete globalThis.window;
			else globalThis.window = originalWindow;
		}
	};
}

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('setTheme fires PUT /api/v1/ui-prefs with the theme body', async () => {
	let seen;
	let body;
	let release;
	const gate = new Promise((resolve) => {
		release = resolve;
	});
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		release();
		return jsonResponse({ theme: 'dark', hide_todo_settings: false });
	};

	prefs.setTheme('light');
	await gate;

	assert.equal(seen.url, `${API_BASE}/api/v1/ui-prefs`);
	assert.equal(seen.method, 'PUT');
	assert.equal(seen.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, { theme: 'light' });
	assert.equal(prefs.uiPrefs.theme, 'light');
});

test('hydrateConfirmPrefsFromDisk GETs ui-prefs and applies fields', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({
			theme: 'light',
			hide_todo_settings: true,
			confirm: { relocate: true },
			auto_sync: { rekordbox: true, djay: false, open_dj: true }
		});
	};

	// Reset local fields that hydrate should overwrite.
	prefs.uiPrefs.theme = 'dark';
	prefs.uiPrefs.hide_todo_settings = false;
	prefs.uiPrefs.confirm = {};

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(seen.url, `${API_BASE}/api/v1/ui-prefs`);
	assert.equal(seen.method, 'GET');
	assert.equal(prefs.uiPrefs.theme, 'light');
	assert.equal(prefs.uiPrefs.hide_todo_settings, true);
	assert.equal(prefs.uiPrefs.confirm.relocate, true);
});

test('hydrateConfirmPrefsFromDisk resolves quietly on non-2xx', async () => {
	prefs.uiPrefs.theme = 'dark';
	prefs.uiPrefs.hide_todo_settings = false;
	const beforeConfirm = { ...prefs.uiPrefs.confirm };

	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: 'nope' }), {
			status: 503,
			statusText: 'Service Unavailable',
			headers: { 'content-type': 'application/json' }
		});

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(prefs.uiPrefs.theme, 'dark');
	assert.equal(prefs.uiPrefs.hide_todo_settings, false);
	assert.deepEqual(prefs.uiPrefs.confirm, beforeConfirm);
});

test('hydrateConfirmPrefsFromDisk resolves quietly when the daemon is unreachable', async () => {
	prefs.uiPrefs.theme = 'dark';
	prefs.uiPrefs.hide_todo_settings = false;

	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(prefs.uiPrefs.theme, 'dark');
	assert.equal(prefs.uiPrefs.hide_todo_settings, false);
});

test('playlist-tree width defaults, persists, and clamps at documented bounds', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		assert.equal(isolated.uiPrefs.playlist_tree_width, 300);
		isolated.setPlaylistTreeWidth(999);
		assert.equal(isolated.uiPrefs.playlist_tree_width, 520);
		assert.equal(JSON.parse(storage.values.get('mdt.rb.ui-prefs.v1')).playlist_tree_width, 520);
		isolated.setPlaylistTreeWidth(1);
		assert.equal(isolated.uiPrefs.playlist_tree_width, 220);
	} finally {
		storage.restore();
	}
});

test('playlist-tree width rejects malformed stored values', async () => {
	const storage = installPrefsStorage(JSON.stringify({ hide_broken_links: false, playlist_tree_width: 521 }));
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/playlist_tree_width must be an integer from 220 through 520/
		);
	} finally {
		storage.restore();
	}
});

// ----- pin 862cd3: MORE/LESS two-deck performance layout -------------------

test('deck layout defaults are more / animate on / 200ms', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		assert.equal(isolated.uiPrefs.deck_layout, 'more');
		assert.equal(isolated.uiPrefs.deck_layout_animate, true);
		assert.equal(isolated.uiPrefs.deck_layout_duration_ms, 200);
	} finally {
		storage.restore();
	}
});

test('deck layout setters persist to storage and round-trip on reload', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		isolated.setDeckLayoutMode('less');
		isolated.setDeckLayoutAnimate(false);
		isolated.setDeckLayoutDurationMs(400);
		assert.equal(isolated.uiPrefs.deck_layout, 'less');
		assert.equal(isolated.uiPrefs.deck_layout_animate, false);
		assert.equal(isolated.uiPrefs.deck_layout_duration_ms, 400);

		const reloaded = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		assert.equal(reloaded.uiPrefs.deck_layout, 'less');
		assert.equal(reloaded.uiPrefs.deck_layout_animate, false);
		assert.equal(reloaded.uiPrefs.deck_layout_duration_ms, 400);
	} finally {
		storage.restore();
	}
});

test('toggleDeckLayoutMode flips more<->less', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		assert.equal(isolated.uiPrefs.deck_layout, 'more');
		isolated.toggleDeckLayoutMode();
		assert.equal(isolated.uiPrefs.deck_layout, 'less');
		isolated.toggleDeckLayoutMode();
		assert.equal(isolated.uiPrefs.deck_layout, 'more');
	} finally {
		storage.restore();
	}
});

test('deck layout rejects an unknown persisted deck_layout value', async () => {
	const storage = installPrefsStorage(
		JSON.stringify({ hide_broken_links: false, deck_layout: 'sideways' })
	);
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/deck_layout must be 'more'\|'less'/
		);
	} finally {
		storage.restore();
	}
});

test('deck layout rejects an unknown persisted deck_layout_animate value', async () => {
	const storage = installPrefsStorage(
		JSON.stringify({ hide_broken_links: false, deck_layout_animate: 'yes' })
	);
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/deck_layout_animate is not a boolean/
		);
	} finally {
		storage.restore();
	}
});

test('deck layout rejects a persisted deck_layout_duration_ms outside the allowed set', async () => {
	const storage = installPrefsStorage(
		JSON.stringify({ hide_broken_links: false, deck_layout_duration_ms: 250 })
	);
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/deck_layout_duration_ms must be one of 0, 100, 200, 300, 400/
		);
	} finally {
		storage.restore();
	}
});

test('setDeckLayoutMode fires PUT /api/v1/ui-prefs with the deck_layout body', async () => {
	let seen;
	let body;
	let release;
	const gate = new Promise((resolve) => {
		release = resolve;
	});
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		release();
		return jsonResponse({ theme: 'dark', hide_todo_settings: false });
	};

	prefs.setDeckLayoutMode('less');
	await gate;

	assert.equal(seen.url, `${API_BASE}/api/v1/ui-prefs`);
	assert.equal(seen.method, 'PUT');
	assert.deepEqual(body, { deck_layout: 'less' });
	assert.equal(prefs.uiPrefs.deck_layout, 'less');
});

test('setDeckLayoutDurationMs rejects a value outside the allowed set', async () => {
	assert.throws(
		() => prefs.setDeckLayoutDurationMs(250),
		/deck layout duration must be one of 0, 100, 200, 300, 400/
	);
});

test('hydrateConfirmPrefsFromDisk applies deck_layout fields from disk', async () => {
	prefs.uiPrefs.deck_layout = 'more';
	prefs.uiPrefs.deck_layout_animate = true;
	prefs.uiPrefs.deck_layout_duration_ms = 200;

	globalThis.fetch = async () =>
		jsonResponse({
			theme: 'dark',
			deck_layout: 'less',
			deck_layout_animate: false,
			deck_layout_duration_ms: 300
		});

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.equal(prefs.uiPrefs.deck_layout, 'less');
	assert.equal(prefs.uiPrefs.deck_layout_animate, false);
	assert.equal(prefs.uiPrefs.deck_layout_duration_ms, 300);
});

// ----- #1475: by-ear level calibration (R/M) --------------------------------

test('level calibration defaults to uncaptured and disabled', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		assert.deepEqual(isolated.uiPrefs.level_calibration, {
			red_dbfs: null,
			red_enabled: false,
			ceiling_dbfs: null,
			ceiling_enabled: false
		});
	} finally {
		storage.restore();
	}
});

test('setLevelCalibrationCapture stores the level and enables, fires PUT', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		let seen;
		let body;
		let release;
		const gate = new Promise((resolve) => {
			release = resolve;
		});
		globalThis.fetch = async (request) => {
			seen = request;
			body = await request.clone().json();
			release();
			return jsonResponse({ theme: 'dark' });
		};

		isolated.setLevelCalibrationCapture('red', -4.2);
		await gate;

		assert.equal(isolated.uiPrefs.level_calibration.red_dbfs, -4.2);
		assert.equal(isolated.uiPrefs.level_calibration.red_enabled, true);
		assert.equal(seen.url, `${API_BASE}/api/v1/ui-prefs`);
		assert.equal(seen.method, 'PUT');
		assert.deepEqual(body.level_calibration, {
			red_dbfs: -4.2,
			red_enabled: true,
			ceiling_dbfs: null,
			ceiling_enabled: false
		});
		assert.equal(
			JSON.parse(storage.values.get('mdt.rb.ui-prefs.v1')).level_calibration.red_dbfs,
			-4.2
		);
	} finally {
		storage.restore();
	}
});

test('setLevelCalibrationDisabled clears the flag but keeps the captured number', async () => {
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		globalThis.fetch = async () => jsonResponse({ theme: 'dark' });

		isolated.setLevelCalibrationCapture('ceiling', -1.0);
		assert.equal(isolated.uiPrefs.level_calibration.ceiling_enabled, true);

		isolated.setLevelCalibrationDisabled('ceiling');

		assert.equal(isolated.uiPrefs.level_calibration.ceiling_enabled, false);
		assert.equal(isolated.uiPrefs.level_calibration.ceiling_dbfs, -1.0);
	} finally {
		storage.restore();
	}
});

test('setLevelCalibrationCapture rejects a non-finite dbfs', async () => {
	assert.throws(() => prefs.setLevelCalibrationCapture('red', Number.NaN), /dbfs must be finite/);
});

test('setLevelCalibrationCapture rejects a capture outside the persisted bounds', async () => {
	// A hot source + max trim + boosted EQ can genuinely produce a post-EQ tap
	// above +12 dBFS. Persisting it would round-trip fine now and then brick
	// the uiPrefs singleton on the NEXT load, since parseLevelCalibration
	// enforces the same bounds with no try/catch around module-scope init.
	assert.throws(
		() => prefs.setLevelCalibrationCapture('red', 20.0),
		/between -60 and 12/
	);
	assert.throws(
		() => prefs.setLevelCalibrationCapture('ceiling', -120.0),
		/between -60 and 0/
	);
});

test('setLevelCalibrationCapture rejects a ceiling above 0 dBFS, but not a red anchor', async () => {
	// min(1, 10**(dbfs/20)) is a no-op attenuation above 0 dBFS, so a ceiling
	// capture there would arm M while leaving the master gain untouched. Red
	// is a meter anchor, not a gain multiplier, so it keeps the wider range.
	assert.throws(
		() => prefs.setLevelCalibrationCapture('ceiling', 2.0),
		/ceiling dbfs must be finite and between -60 and 0/
	);
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
		globalThis.fetch = async () => jsonResponse({ theme: 'dark' });
		isolated.setLevelCalibrationCapture('red', 2.0);
		assert.equal(isolated.uiPrefs.level_calibration.red_dbfs, 2.0);
		assert.equal(isolated.uiPrefs.level_calibration.red_enabled, true);
	} finally {
		storage.restore();
	}
});

test('level calibration rejects a persisted enabled flag with no captured level', async () => {
	const storage = installPrefsStorage(
		JSON.stringify({
			hide_broken_links: false,
			level_calibration: { red_dbfs: null, red_enabled: true, ceiling_dbfs: null, ceiling_enabled: false }
		})
	);
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/level_calibration.red_enabled requires red_dbfs to be set/
		);
	} finally {
		storage.restore();
	}
});

test('level calibration rejects a persisted level outside -60..12 dBFS', async () => {
	const storage = installPrefsStorage(
		JSON.stringify({
			hide_broken_links: false,
			level_calibration: { red_dbfs: 13, red_enabled: false, ceiling_dbfs: null, ceiling_enabled: false }
		})
	);
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE }),
			/level_calibration.red_dbfs must be a finite number between -60 and 12/
		);
	} finally {
		storage.restore();
	}
});

test('hydrateConfirmPrefsFromDisk applies level_calibration from disk', async () => {
	prefs.uiPrefs.level_calibration = {
		red_dbfs: null,
		red_enabled: false,
		ceiling_dbfs: null,
		ceiling_enabled: false
	};

	globalThis.fetch = async () =>
		jsonResponse({
			theme: 'dark',
			level_calibration: { red_dbfs: -2.5, red_enabled: true, ceiling_dbfs: -0.5, ceiling_enabled: true }
		});

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(prefs.uiPrefs.level_calibration, {
		red_dbfs: -2.5,
		red_enabled: true,
		ceiling_dbfs: -0.5,
		ceiling_enabled: true
	});
});

// Codex P2 then P1 BLOCKING on #1503: each R/M action PUTs a full snapshot of
// BOTH halves, so two quick clicks could land out of order and an older
// snapshot could overwrite a newer one. The tab looked right; the lost toggle
// came back after a reload.
//
// This runs against a REAL http server and the real `api.PUT` client rather
// than a replaced `globalThis.fetch`, because a fabricated transport success
// cannot evidence request ordering (AGENTS.md, "No mocks and locked real
// fixtures"). The server is a recorder, not a stand-in for anything under
// test: the behaviour being pinned is entirely client-side write ordering.
//
// Regression line: if calibration writes stop being chained then the LAST
// click is not the last write, and a capture silently reverts on reload.
test('calibration writes are serialized over a real http transport', async () => {
	const { createServer } = await import('node:http');
	const bodies = [];
	const releases = [];
	let inFlight = 0;
	let maxInFlight = 0;

	const server = createServer((req, res) => {
		let raw = '';
		req.on('data', (c) => (raw += c));
		req.on('end', () => {
			inFlight += 1;
			maxInFlight = Math.max(maxInFlight, inFlight);
			bodies.push(JSON.parse(raw));
			// Hold the response open so a parallel implementation would overlap.
			releases.push(() => {
				inFlight -= 1;
				res.writeHead(200, { 'content-type': 'application/json' });
				res.end(JSON.stringify({ theme: 'dark' }));
			});
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const base = `http://127.0.0.1:${server.address().port}`;

	// Earlier tests in this file replace globalThis.fetch and do not all restore
	// it. This test needs the REAL fetch or it silently talks to a leftover
	// stub instead of the server above, which is how it first failed.
	globalThis.fetch = originalFetch;
	const storage = installPrefsStorage();
	try {
		const isolated = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', {
			viteApiBase: base
		});

		isolated.setLevelCalibrationCapture('red', -12);
		isolated.setLevelCalibrationCapture('ceiling', -3);
		isolated.setLevelCalibrationDisabled('red');

		for (let i = 0; i < 3; i += 1) {
			const deadline = Date.now() + 10_000;
			while (releases.length === 0) {
				if (Date.now() > deadline) throw new Error(`write ${i + 1} never reached the server`);
				await new Promise((r) => setTimeout(r, 5));
			}
			releases.shift()();
			await new Promise((r) => setTimeout(r, 20));
		}

		assert.equal(maxInFlight, 1, `writes overlapped (${maxInFlight} in flight at once)`);
		assert.equal(bodies.length, 3, 'every action must still reach the daemon');
		const last = bodies[bodies.length - 1].level_calibration;
		assert.equal(last.red_enabled, false, 'last write lost the disable');
		assert.equal(last.ceiling_dbfs, -3, 'last write lost the ceiling capture');
	} finally {
		storage.restore();
		await new Promise((resolve) => server.close(resolve));
	}
});
