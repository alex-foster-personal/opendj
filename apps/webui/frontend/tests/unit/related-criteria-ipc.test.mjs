// requirement: LIBUX-10046
// PRRT_kwDOSEvNd861J-iH: criteria edits must land on the typed bus so agents
// update uiPrefs and a mounted Related tab refetches without bypassing IPC.
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://related-criteria-ipc.example.test';
const DEFAULTS = {
	bpm_percent: 6,
	key: true,
	genre: true,
	color: false,
	rating: false,
	mytag: false,
	artist: false
};

let ipc;
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
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
	delete globalThis.window;
});

beforeEach(() => {
	prefs.uiPrefs.library_related_criteria = { ...DEFAULTS };
});

test('browser_related_criteria persists through IPC and reads back on ui', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	let body;
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		return jsonResponse({});
	};
	try {
		await ipc.dispatchPerformanceCommand({
			type: 'browser_related_criteria',
			bpm_percent: 4,
			artist: true
		});
		const expected = { ...DEFAULTS, bpm_percent: 4, artist: true };
		assert.deepEqual(body, { library_related_criteria: expected });
		assert.deepEqual(ipc.queryPerformanceState().ui.library_related_criteria, expected);
	} finally {
		uninstall();
	}
});

test('browser_related_criteria_reset restores defaults on ui', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	prefs.uiPrefs.library_related_criteria = { ...DEFAULTS, color: true, artist: true };
	let body;
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		return jsonResponse({});
	};
	try {
		await ipc.dispatchPerformanceCommand({ type: 'browser_related_criteria_reset' });
		assert.deepEqual(body, { library_related_criteria: { ...DEFAULTS } });
		assert.deepEqual(ipc.queryPerformanceState().ui.library_related_criteria, { ...DEFAULTS });
	} finally {
		uninstall();
	}
});

test('a failed criteria PUT rejects and leaves readback unchanged', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	const before = { ...prefs.uiPrefs.library_related_criteria };
	globalThis.fetch = async () => jsonResponse({ detail: 'nope' }, 500);
	try {
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'browser_related_criteria', genre: false }),
			/nope/
		);
		assert.deepEqual(ipc.queryPerformanceState().ui.library_related_criteria, before);
	} finally {
		uninstall();
	}
});
