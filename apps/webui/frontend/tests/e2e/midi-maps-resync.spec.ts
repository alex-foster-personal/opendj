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

/**
 * Dev-server URLs for the modules the in-page callbacks below import.
 *
 * `page.evaluate` and `page.waitForFunction` bodies run in the browser, where
 * `/src/lib/...` is a URL vite serves, not a module specifier resolvable from
 * disk. TypeScript resolves a rooted specifier by PATH and never consults an
 * ambient `declare module`, so the URLs cannot be declared away; written as
 * string literals they produced seven `Cannot find module '/src/...'` errors
 * under `svelte-check`, which is what kept the frontend job red once the unit
 * tests ahead of it started passing again.
 *
 * Typing the values as `string` (not `as const`) is what stops that: `import()`
 * on a non-literal yields `any`, so nothing is resolved at build time. The
 * `as typeof import(...)` at each call site puts the types back, resolved from
 * the real module, so renaming `getConnectionState`, `resolveMapForPort`,
 * `requestMidiAccess` or `midiUi` still fails this check.
 */
const SRC: Record<'eventsBus' | 'webmidi' | 'midiUiState', string> = {
	eventsBus: '/src/lib/api/events-bus.ts',
	webmidi: '/src/lib/rb/midi/webmidi.svelte.ts',
	midiUiState: '/src/lib/components/rb/midi/midi-ui-state.svelte.ts'
};

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
	await page.waitForFunction(async (url) => {
		const { getConnectionState } = (await import(url)) as typeof import('$lib/api/events-bus');
		return getConnectionState() === 'open';
	}, SRC.eventsBus);

	// Arm installed-map listeners (same entry point the MIDI panel uses).
	await page.evaluate(async (url) => {
		const { requestMidiAccess } = (await import(
			url
		)) as typeof import('$lib/components/rb/midi/midi-ui-state.svelte');
		await requestMidiAccess();
	}, SRC.midiUiState);

	const putA = await page.request.put(`${API}/api/v1/midi/maps/aaa-device`, { data: DOC_A });
	expect(putA.status()).toBe(200);

	await page.waitForFunction(async (url) => {
		const { resolveMapForPort } = (await import(url)) as typeof import('$lib/rb/midi/webmidi.svelte');
		return resolveMapForPort('AaaDevice 1') !== null;
	}, SRC.webmidi);

	// Disconnect the page's real WebSocket; the bus schedules reconnect.
	await context.setOffline(true);

	const putB = await page.request.put(`${API}/api/v1/midi/maps/bbb-device`, { data: DOC_B });
	expect(putB.status()).toBe(200);

	// Map B was installed while the socket was down - not yet in the page registry.
	await page.waitForFunction(async (url) => {
		const { resolveMapForPort } = (await import(url)) as typeof import('$lib/rb/midi/webmidi.svelte');
		return resolveMapForPort('BbbDevice 1') === null;
	}, SRC.webmidi);

	await context.setOffline(false);

	await page.waitForFunction(async (url) => {
		const { getConnectionState } = (await import(url)) as typeof import('$lib/api/events-bus');
		return getConnectionState() === 'open';
	}, SRC.eventsBus);

	await page.waitForFunction(async (url) => {
		const { resolveMapForPort } = (await import(url)) as typeof import('$lib/rb/midi/webmidi.svelte');
		return resolveMapForPort('BbbDevice 1') !== null;
	}, SRC.webmidi);

	const installedMapsError = await page.evaluate(async (url) => {
		const { midiUi } = (await import(
			url
		)) as typeof import('$lib/components/rb/midi/midi-ui-state.svelte');
		return midiUi.installedMapsError;
	}, SRC.midiUiState);
	expect(installedMapsError).toBeNull();
});
