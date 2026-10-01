import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import type { PerformanceCommand } from '../../src/lib/rb/performance-ipc.svelte';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const STEMS = ['drums', 'bass', 'other', 'vocal'] as const;

async function realDemucsTrack(request: APIRequestContext): Promise<string> {
	const requested = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK;
	let ids = requested === undefined ? [] : [requested];
	if (requested === undefined) {
		const response = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true`);
		expect(response.ok()).toBe(true);
		const tracks = await response.json() as { items: { stable_id: string; file_exists: boolean }[] };
		ids = tracks.items.filter((track) => track.file_exists).map((track) => track.stable_id);
	}
	for (const id of ids) {
		const response = await request.get(`${API_BASE}/api/v1/tracks/${id}/stems`);
		expect(response.ok(), `manifest must be readable for ${id}`).toBe(true);
		const manifest = await response.json() as { schema?: number; layout?: string };
		if (manifest.schema === 1 && manifest.layout === 'demucs4') return id;
	}
	throw new Error('Mixtour Neural Mix requires a real available demucs4 bundle');
}

async function dispatch(page: Page, command: PerformanceCommand) {
	return page.evaluate((command) => {
		if (window.musicDjToolsPerformance === undefined) throw new Error('performance IPC unavailable');
		return window.musicDjToolsPerformance.dispatch(command);
	}, command);
}

async function state(page: Page) {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.query().command_pending === false);
	return page.evaluate(() => window.musicDjToolsPerformance!.query());
}

/** Supersedes the fabricated deck/window/storage writer test in
 * performance-deeplink.test.mjs. Uses real captured state and browser APIs. */
async function verifySessionWriter(page: Page, stableId: string) {
	const writer = await page.evaluateHandle(async (url) => {
		const { createSessionSnapshotWriter } = await import(url) as typeof import('../../src/lib/rb/performance-session.svelte');
		return createSessionSnapshotWriter({ now: Date.now, storage: sessionStorage,
			location: window.location, replaceState: (url) => history.replaceState(null, '', url),
			query: () => window.musicDjToolsPerformance!.query(), isLive: () => true,
			operatorMaster: () => null, document, window });
	}, '/src/lib/rb/performance-session.svelte.ts');
	const first = await writer.evaluate((writer) => {
		writer.flush(true);
		const first = sessionStorage.getItem('mdt.rb.performance-session.v1');
		writer.flush(false);
		return { first, immediate: sessionStorage.getItem('mdt.rb.performance-session.v1') };
	});
	expect(first.first).not.toBeNull();
	expect(first.immediate).toBe(first.first);
	expect(new URL(page.url()).searchParams.get('d1')).toBe(stableId);
	const read = () => page.evaluate(() => sessionStorage.getItem('mdt.rb.performance-session.v1'));
	// The first interval can fall just short of one throttle window after
	// the forced write; the following interval must persist the real state.
	await expect.poll(read, { timeout: 25_000 }).not.toBe(first.first);
	const periodic = await read();
	await page.waitForFunction((previous) => Date.now() > JSON.parse(previous!).captured_at_ms, periodic);
	// A real DOM pagehide event reaches the production handler, not a fake listener map.
	await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')));
	expect(await read()).not.toBe(periodic);
	await writer.evaluate((writer) => writer.dispose());
	await writer.dispose();
}

for (const deck of [1, 2, 3, 4] as const) {
	test(`Mixtour Neural Mix pads drive real isolated stems on deck ${deck}`, async ({ page, request }, info) => {
		test.setTimeout(180_000);
		const stableId = await realDemucsTrack(request);
		await page.setViewportSize({ width: 1280, height: 800 });
		await page.goto('/performance');
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await dispatch(page, { type: 'master_volume', value: 0.1 });
		const controller = await page.evaluateHandle(async ({ deck, glueUrl, mapUrl }) => {
			const glue = await import(glueUrl) as typeof import('../../src/lib/rb/midi/action-glue.svelte');
			const { RELOOP_MIXTOUR_PRO_MAP: map } = await import(mapUrl) as typeof import('../../src/lib/rb/midi/maps/reloop-mixtour-pro');
			return {
				send(note: number, pressed: boolean) {
					const binding = map.bindings.find((binding) => binding.source.ch === deck + 4 &&
						binding.source.kind === 'note' && binding.source.id === note);
					if (binding === undefined) throw new Error(`missing factory pad binding ${deck}:${note}`);
					glue.handleMidiAction(binding.action, { kind: 'button', pressed, velocity: pressed ? 127 : 0 }, 'mixtour-neural-proof');
				},
				leds: () => glue.midiLedFeedback(map, 'mixtour-neural-proof')
					.filter((output) => output.ch === deck + 4 && output.note >= 0x14 && output.note <= 0x23)
			};
		}, { deck, glueUrl: '/src/lib/rb/midi/action-glue.svelte.ts', mapUrl: '/src/lib/rb/midi/maps/reloop-mixtour-pro.ts' });
		const send = async (note: number, pressed = true) => {
			await controller.evaluate((controller, args) => controller.send(args.note, args.pressed), { note, pressed });
			return state(page);
		};
		const lights = async (solo: number | null, active = [0, 1, 2, 3]) => {
			const outputs = await controller.evaluate((controller) => controller.leds());
			expect(outputs).toHaveLength(16);
			for (const output of outputs) {
				const pad = (output.note - 0x14) % 8;
				expect(output.velocity).toBe(pad < 4 ? (active.includes(pad) ? 120 : 56) : (solo === pad - 4 ? 127 : 63));
			}
		};
		await send(0x07);
		await expect(page.getByText(`Deck ${deck}: Neural Mix pads require four ready stems (unavailable)`, { exact: true })).toBeVisible();
		await dispatch(page, { type: 'load', deck, stable_id: stableId });
		await expect.poll(async () => (await state(page)).decks[deck].stems.status, { timeout: 60_000 }).toBe('ready');
		expect((await state(page)).decks[deck].stems.available_controls).toEqual(expect.arrayContaining([...STEMS]));
		await lights(null);
		await expect(page.getByTestId(`stem-bass-deck-${deck}`)).toBeVisible();
		await expect(page.getByTestId(`stem-other-deck-${deck}`)).toBeVisible();
		for (const stem of [...STEMS, 'instrumental']) {
			const bounds = await page.getByTestId(`stem-${stem}-channel-${deck}`).evaluate((chip) => {
				const slot = chip.closest('.strip-slot');
				if (slot === null) throw new Error('missing visible mixer strip');
				const outer = slot.getBoundingClientRect();
				const inner = chip.getBoundingClientRect();
				return { fits: inner.top >= outer.top && inner.bottom <= outer.bottom &&
					inner.left >= outer.left && inner.right <= outer.right,
					textFits: chip.scrollWidth <= chip.clientWidth + 1 };
			});
			expect(bounds, `mixer ${stem} must not be clipped`).toEqual({ fits: true, textFits: true });
		}
		for (const [index, stem] of STEMS.entries()) {
			let current = await send(0x14 + index);
			expect(current.decks[deck].stems.controls[stem].muted).toBe(true);
			await lights(null, [0, 1, 2, 3].filter((value) => value !== index));
			current = await send(0x14 + index, false);
			expect(current.decks[deck].stems.controls[stem].muted).toBe(true);
			current = await send(0x1c + index); // SHIFT repeats the same factory action
			expect(current.decks[deck].stems.controls[stem].muted).toBe(false);
		}
		for (const [index, stem] of STEMS.entries()) {
			const current = await send(0x18 + index);
			expect(Object.entries(current.decks[deck].stems.controls).filter(([, control]) => control.solo).map(([name]) => name)).toEqual([stem]);
			await lights(index, [index]);
			await send(0x18 + index, false);
		}
		await send(0x23); // SHIFT + final solo toggles exclusive solo off
		await lights(null);
		await dispatch(page, { type: 'seek', deck, position_ms: 60_000 });
		await dispatch(page, { type: 'play', deck, playing: true });
		await page.waitForFunction((deck) => {
			const current = window.musicDjToolsPerformance?.query().decks[deck];
			return current?.audible && !current.transport_pending &&
				current.transport_clock.desired_revision === current.transport_clock.presented_revision;
		}, deck);
		const rms = () => page.evaluate((deck) => {
			const samples = window.musicDjToolsPerformance!.capture(deck).time_domain;
			return Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
		}, deck);
		await expect.poll(rms).toBeGreaterThan(0.0001);
		for (let index = 0; index < 4; index++) await send(0x14 + index);
		await expect.poll(rms).toBeLessThan(0.00001);
		// Stay deliberately silent beyond the two-second dropout watchdog.
		const silentPosition = (await state(page)).decks[deck].position_ms;
		await expect.poll(async () => {
			const current = (await state(page)).decks[deck];
			expect(current.playing).toBe(true);
			return current.position_ms;
		}).toBeGreaterThan(silentPosition + 3_000);
		await expect.poll(rms).toBeLessThan(0.00001);
		await lights(null, []);
		await send(0x15); // bass alone, not the grouped instrumental signal
		await expect.poll(rms).toBeGreaterThan(0.0001);
		await lights(null, [1]);
		await page.getByTestId(`stem-bass-channel-${deck}`).click();
		await expect.poll(rms).toBeLessThan(0.00001);
		await lights(null, []);
		const saved = await page.evaluate(async ({ deck, url }) => {
			const { buildRescueSnapshot } = await import(url) as typeof import('../../src/lib/rb/rescue-snapshot');
			return buildRescueSnapshot(window.musicDjToolsPerformance!.query(), 'periodic', Date.now(),
				{ 1: null, 2: null, 3: null, 4: null }).decks[deck].stems;
		}, { deck, url: '/src/lib/rb/rescue-snapshot.ts' });
		expect(saved.bass?.muted).toBe(true);
		expect(saved.other?.muted).toBe(true);
		const session = await page.evaluate(async ({ deck, writerUrl, parserUrl }) => {
			const { buildPerformanceSessionSnapshot } = await import(writerUrl) as typeof import('../../src/lib/rb/performance-session.svelte');
			const { parsePerformanceSession } = await import(parserUrl) as typeof import('../../src/lib/rb/performance-session-snapshot');
			const raw = buildPerformanceSessionSnapshot(window.musicDjToolsPerformance!.query(), Date.now(), null);
			const parsed = parsePerformanceSession(raw);
			// Disposable transformations of this real capture, never fixture edits.
			const legacy = JSON.parse(raw);
			for (const controls of Object.values(legacy.stems) as Record<string, unknown>[]) {
				delete controls.bass;
				delete controls.other;
			}
			const legacyParsed = parsePerformanceSession(JSON.stringify(legacy));
			const malformed = JSON.parse(raw);
			malformed.stems[deck].bass.gain = 'invalid';
			return { current: parsed?.stems[deck], legacy: legacyParsed?.stems[deck],
				malformed: parsePerformanceSession(JSON.stringify(malformed)) };
		}, { deck, writerUrl: '/src/lib/rb/performance-session.svelte.ts', parserUrl: '/src/lib/rb/performance-session-snapshot.ts' });
		expect(session.current?.bass?.muted).toBe(true);
		expect(session.current?.other?.muted).toBe(true);
		expect(session.legacy?.bass).toBeUndefined();
		expect(session.legacy?.instrumental).toEqual(session.current?.instrumental);
		expect(session.malformed).toBeNull();
		if (deck === 1) await verifySessionWriter(page, stableId);
		await page.screenshot({ path: info.outputPath('neural-stems.png'), fullPage: true });
		await dispatch(page, { type: 'play', deck, playing: false });
		await dispatch(page, { type: 'unload', deck });
		await controller.dispose();
	});
}
