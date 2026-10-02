// PR #4014 (Sol P1 on prefs.svelte.ts patchCompatibleFilter): a compatible
// filter range change must use the verified disk write, commit the live pref
// only after the PUT succeeds, and reject (leaving the committed ranges as they
// were) on an HTTP error or a transport failure. Both directions are pinned: a
// failed write must not commit, and a successful one must.
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://compat-filter-save.example.test';
const BEFORE = {
	camelot_steps: 1,
	bpm_enabled: true,
	bpm_window_bpm: 10,
	bpm_direction: 'both'
};

let prefs;
let originalFetch;

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
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

beforeEach(() => {
	prefs.uiPrefs.compatible_filter = { ...BEFORE };
});

test('a 2xx PUT commits the new range after the write and sends the full object', async () => {
	let body;
	let committedDuringPut;
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		committedDuringPut = { ...prefs.uiPrefs.compatible_filter };
		return jsonResponse({});
	};

	await prefs.patchCompatibleFilter({ camelot_steps: 2 });

	assert.deepEqual(body, { compatible_filter: { ...BEFORE, camelot_steps: 2 } });
	assert.deepEqual(committedDuringPut, BEFORE, 'the pref must not commit before the PUT lands');
	assert.deepEqual(prefs.uiPrefs.compatible_filter, { ...BEFORE, camelot_steps: 2 });
});

test('an HTTP error rejects and keeps the committed range', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'WRITE_FAILED', message: 'disk full' } }, 500);

	await assert.rejects(prefs.patchCompatibleFilter({ bpm_enabled: false }), /disk full/);
	assert.deepEqual(prefs.uiPrefs.compatible_filter, BEFORE);
});

test('a transport failure rejects and keeps the committed range', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('Failed to fetch');
	};

	await assert.rejects(prefs.patchCompatibleFilter({ bpm_direction: 'above' }), /Failed to fetch/);
	assert.deepEqual(prefs.uiPrefs.compatible_filter, BEFORE);
});

test('two quick range clicks compose, and a failed click never rides on a queued one', async () => {
	// Sol P2 r4167041284: the second click is queued WHILE the first PUT is in
	// flight; the first then fails. The second must be built from the committed
	// ranges when it runs, so it carries only its own change.
	const bodies = [];
	let failFirst;
	const firstGate = new Promise((resolve) => {
		failFirst = resolve;
	});
	globalThis.fetch = async (request) => {
		const body = await request.clone().json();
		bodies.push(body);
		if (bodies.length === 1) {
			await firstGate;
			throw new TypeError('Failed to fetch');
		}
		return jsonResponse({});
	};

	const first = prefs.patchCompatibleFilter({ bpm_window_bpm: 20 });
	const second = prefs.patchCompatibleFilter({ bpm_direction: 'below' });
	failFirst();
	await assert.rejects(first, /Failed to fetch/);
	await second;

	assert.deepEqual(bodies, [
		{ compatible_filter: { ...BEFORE, bpm_window_bpm: 20 } },
		{ compatible_filter: { ...BEFORE, bpm_direction: 'below' } }
	]);
	assert.deepEqual(prefs.uiPrefs.compatible_filter, { ...BEFORE, bpm_direction: 'below' });

	// Control: two successful clicks still compose.
	bodies.length = 0;
	globalThis.fetch = async (request) => {
		bodies.push(await request.clone().json());
		return jsonResponse({});
	};
	await Promise.all([
		prefs.patchCompatibleFilter({ camelot_steps: 0 }),
		prefs.patchCompatibleFilter({ bpm_enabled: false })
	]);
	assert.deepEqual(prefs.uiPrefs.compatible_filter, {
		...BEFORE,
		bpm_direction: 'below',
		camelot_steps: 0,
		bpm_enabled: false
	});
});
