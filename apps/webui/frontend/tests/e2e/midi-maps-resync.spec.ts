/**
 * Issue #1013 / ADR-0120: installed MIDI maps reload after a real WebSocket
 * disconnect/reconnect on /api/v1/events.
 *
 * Follow-up from PR #511 review thread discussion_r3920171815 ("Exercise
 * resync through real browser interfaces"). The unit test
 * (midi-panel-resync.test.mjs) proves subscribeKind + subscribeResync wiring
 * through events-bus.ts's documented socketFactory seam; this spec closes the
 * real-socket gap through the page's own WebSocket (Vite proxy ws: true ->
 * engine WsHub).
 *
 * Regression line:
 *   if reconnect after a missed PUT does not reload installed maps then broken
 */
import { expect, test } from '@playwright/test';

const API = process.env.MIDI_MAPS_E2E_API_BASE ?? '';

const DOC_A = {
	schemaVersion: 1,
	id: 'aaa-device',
	vendor: 'AaaCo',
	model: 'AaaDevice 1',
	nameMatch: 'AaaDevice',
	bindings: [
		{
			source: { ch: 1, kind: 'note', id: 11 },
			action: { type: 'deck_play_toggle', deck: 1 },
			provenance: { tier: 'learned', cite: 'learn wizard, port AaaDevice', verified: true }
		}
	]
};

const DOC_B = {
	...DOC_A,
	id: 'bbb-device',
	vendor: 'BbbCo',
	nameMatch: 'BbbDevice'
};

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => {
		Object.defineProperty(navigator, 'requestMIDIAccess', {
			configurable: true,
			writable: true,
			value: async () => ({ inputs: new Map(), outputs: new Map(), onstatechange: null })
		});
	});
});

test.afterEach(async ({ request }) => {
	for (const id of ['aaa-device', 'bbb-device']) {
		const r = await request.delete(`${API}/api/v1/midi/maps/${id}`);
		if (!r.ok() && r.status() !== 404) {
			throw new Error(`teardown DELETE ${id} failed ${r.status()}: ${await r.text()}`);
		}
	}
});

test('installed maps reload after real WebSocket disconnect/reconnect', async ({ page, context }) => {
	expect(API, 'the config must publish MIDI_MAPS_E2E_API_BASE').not.toBe('');

	await page.goto('/performance');

	// Preflight must clear before the app shell (and events bus) is live.
	await expect(page.getByRole('button', { name: 'Jobs drawer' })).toBeEnabled();

	// +layout.svelte calls connectEventsBus(); wait for the real socket to open.
	await page.waitForFunction(async () => {
		const { getConnectionState } = await import('/src/lib/api/events-bus.ts');
		return getConnectionState() === 'open';
	});

	// Arm installed-map listeners (same entry point the MIDI panel uses).
	await page.evaluate(async () => {
		const { requestMidiAccess } = await import(
			'/src/lib/components/rb/midi/midi-ui-state.svelte.ts'
		);
		await requestMidiAccess();
	});

	const putA = await page.request.put(`${API}/api/v1/midi/maps/aaa-device`, { data: DOC_A });
	expect(putA.status()).toBe(200);

	await page.waitForFunction(async () => {
		const { resolveMapForPort } = await import('/src/lib/rb/midi/webmidi.svelte.ts');
		return resolveMapForPort('AaaDevice 1') !== null;
	});

	// Disconnect the page's real WebSocket; the bus schedules reconnect.
	await context.setOffline(true);

	const putB = await page.request.put(`${API}/api/v1/midi/maps/bbb-device`, { data: DOC_B });
	expect(putB.status()).toBe(200);

	// Map B was installed while the socket was down - not yet in the page registry.
	await page.waitForFunction(async () => {
		const { resolveMapForPort } = await import('/src/lib/rb/midi/webmidi.svelte.ts');
		return resolveMapForPort('BbbDevice 1') === null;
	});

	await context.setOffline(false);

	await page.waitForFunction(async () => {
		const { getConnectionState } = await import('/src/lib/api/events-bus.ts');
		return getConnectionState() === 'open';
	});

	await page.waitForFunction(async () => {
		const { resolveMapForPort } = await import('/src/lib/rb/midi/webmidi.svelte.ts');
		return resolveMapForPort('BbbDevice 1') !== null;
	});

	const installedMapsError = await page.evaluate(async () => {
		const { midiUi } = await import('/src/lib/components/rb/midi/midi-ui-state.svelte.ts');
		return midiUi.installedMapsError;
	});
	expect(installedMapsError).toBeNull();
});
