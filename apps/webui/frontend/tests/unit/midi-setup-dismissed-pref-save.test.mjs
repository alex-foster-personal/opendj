// Issue #10076: addMidiSetupDismissedPort syncs the full list to disk.
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://midi-dismiss.example.test';

let prefs;
let originalFetch;
let putBodies;

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
	prefs.uiPrefs.midi_setup_dismissed_ports = [];
	putBodies = [];
	globalThis.fetch = async (input, init) => {
		const url = typeof input === 'string' ? input : input.url;
		const method = init?.method ?? (typeof input === 'object' ? input.method : 'GET');
		if (String(url).includes('/api/v1/ui-prefs') && method === 'PUT') {
			const req = typeof input === 'string' ? new Request(input, init) : input;
			const body = await req.clone().json();
			putBodies.push(body);
			return new Response(JSON.stringify(body), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
});

test('addMidiSetupDismissedPort records a PUT with the port name', async () => {
	prefs.addMidiSetupDismissedPort('DDJ-FLX4');
	await new Promise((r) => setTimeout(r, 50));
	assert.deepEqual(prefs.uiPrefs.midi_setup_dismissed_ports, ['DDJ-FLX4']);
	assert.deepEqual(putBodies, [{ midi_setup_dismissed_ports: ['DDJ-FLX4'] }]);
});
