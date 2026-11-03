/**
 * Requirements: STRUCT-05.
 *
 * The section overlay painter (wave/sections.ts) is loaded on demand by
 * wave/sections-lazy.svelte.ts so the default-OFF overlay does not weigh on
 * the "/" first-paint bundle (bundle budget `library`).
 *
 * Regression lines:
 * - if a surface imports sections.ts statically again then the label tables
 *   and painter rejoin the library first paint for every user
 * - if the pref being OFF still fetches the painter then the deferral saves
 *   nothing
 * - if the loaded module is not cached then every frame re-imports it
 * - if a failed fetch is cached then the overlay can never recover without a
 *   reload
 * - if a failed fetch is retried on the next call then a playing deck, which
 *   redraws every frame, re-requests the dead chunk 60 times a second
 * - if the retry waits for a redraw then a surface that only repaints on
 *   events (a stopped deck, the preview strip) never recovers
 * - if drawWaveRow with the pref ON paints nothing after the painter lands
 *   then toggling the pref on shows no bands until a reload
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = 'tests/unit/fixtures/wave-render-sections-entry.ts';
const flush = () => new Promise((resolve) => setImmediate(resolve));

describe('sections-lazy loader', () => {
	it('OFF fetches nothing; ON returns null until the module lands, then the cached module', async () => {
		const mod = await loadTypeScriptModule(ENTRY);
		for (let i = 0; i < 3; i += 1) assert.equal(mod.sectionsApiFor(false), null);
		await flush();
		// CONTROL for the OFF claim: had OFF started the fetch, ON would now
		// return the module instead of null.
		assert.equal(mod.sectionsApiFor(true), null, 'pref OFF must not have loaded the painter');
		const first = mod.loadSectionsApi();
		assert.equal(mod.loadSectionsApi(), first, 'one in-flight fetch, shared');
		const api = await first;
		assert.equal(typeof api.sectionSpans, 'function');
		assert.equal(typeof api.drawSectionBands, 'function');
		assert.equal(mod.sectionsApiFor(true), api, 'the loaded module is cached');
		assert.equal(mod.sectionsApiFor(false), null, 'pref OFF still paints nothing once loaded');
	});

	it('a failed fetch rejects loudly and is retried, never cached', async () => {
		const mod = await loadTypeScriptModule(ENTRY, { failImports: [/\/sections$/] });
		const first = mod.loadSectionsApi();
		await assert.rejects(first, /Failed to fetch dynamically imported module/);
		const second = mod.loadSectionsApi();
		assert.notEqual(second, first, 'a rejected fetch must not be cached');
		// The retry's own outcome is not asserted: esbuild's test bundle caches
		// the failing module after its first evaluation, where a browser would
		// re-fetch the chunk.
		await second.catch(() => {});
		assert.equal(mod.sectionsApiFor(false), null);
	});

	it('after a failed fetch, per-frame calls back off instead of refetching', async () => {
		const mod = await loadTypeScriptModule(ENTRY, { failImports: [/\/sections$/] });
		const realNow = Date.now;
		const realError = console.error;
		const realSetTimeout = globalThis.setTimeout;
		const timers = [];
		let now = 1_000_000;
		let errors = 0;
		const settle = async () => {
			for (let i = 0; i < 5; i += 1) await flush();
		};
		Date.now = () => now;
		// Recorded, not run: this test drives retries by the clock alone.
		globalThis.setTimeout = (fn, ms) => {
			timers.push(ms);
			return timers.length;
		};
		console.error = () => {
			errors += 1;
		};
		try {
			assert.equal(mod.sectionsLoadError(), null, 'CONTROL: no error before any fetch');
			assert.equal(mod.sectionsApiFor(true), null);
			await settle();
			assert.equal(errors, 1, 'first failure is reported');
			assert.match(mod.sectionsLoadError() ?? '', /Failed to fetch dynamically imported module/, 'the UI can see the failure');
			assert.deepEqual(timers, [1_000], 'the failure itself scheduled a retry, before any other call');
			// Sixty frames inside the backoff window start no new fetch. A retry
			// would show: esbuild's test bundle resolves a re-import of the
			// failing module (see the test above), so the painter would land.
			for (let i = 0; i < 60; i += 1) assert.equal(mod.sectionsApiFor(true), null);
			await settle();
			assert.equal(mod.sectionsApiFor(true), null, 'no refetch inside the backoff window');
			assert.deepEqual(timers, [1_000], 'the failed wanted fetch scheduled one retry for its window');
			assert.equal(errors, 1);
			// CONTROL: past the window the loader does retry, and the retry lands.
			now += 1_001;
			assert.equal(mod.sectionsApiFor(true), null);
			await settle();
			assert.notEqual(mod.sectionsApiFor(true), null, 'retries once the window has passed');
			assert.equal(mod.sectionsLoadError(), null, 'a successful retry clears the error');
		} finally {
			Date.now = realNow;
			globalThis.setTimeout = realSetTimeout;
			console.error = realError;
		}
	});

	it('a failed fetch schedules its own retry, so a surface that never redraws still recovers', async () => {
		const mod = await loadTypeScriptModule(ENTRY, { failImports: [/\/sections$/] });
		const realSetTimeout = globalThis.setTimeout;
		const realError = console.error;
		const timers = [];
		globalThis.setTimeout = (fn, ms) => {
			timers.push({ fn, ms });
			return timers.length;
		};
		console.error = () => {};
		try {
			// CONTROL: a failure nobody asked for schedules nothing.
			await mod.loadSectionsApi().catch(() => {});
			assert.equal(timers.length, 0, 'no surface wanted the painter, so no retry timer');
			// A surface asks inside the backoff window: no fetch now, but a
			// timer for the rest of the window, once however often it asks.
			for (let i = 0; i < 3; i += 1) assert.equal(mod.sectionsApiFor(true), null);
			for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r));
			assert.equal(timers.length, 1, 'one retry timer, not one per call');
			assert.ok(timers[0].ms > 0 && timers[0].ms <= 1_000, `timer covers the rest of the window (${timers[0].ms} ms)`);
			// No further call from any surface: the timer alone retries, and
			// the retry lands (esbuild's test bundle resolves a re-import).
			timers[0].fn();
			for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r));
			assert.notEqual(mod.sectionsApiFor(true), null, 'the scheduled retry loaded the painter');
			assert.equal(mod.sectionsLoadError(), null);
		} finally {
			globalThis.setTimeout = realSetTimeout;
			console.error = realError;
		}
	});

	it('frames while the fetch is in flight add no logger: one failure logs once', async () => {
		const mod = await loadTypeScriptModule(ENTRY, { failImports: [/\/sections$/] });
		const realError = console.error;
		const realSetTimeout = globalThis.setTimeout;
		let errors = 0;
		console.error = () => {
			errors += 1;
		};
		globalThis.setTimeout = () => 0;
		try {
			for (let i = 0; i < 30; i += 1) assert.equal(mod.sectionsApiFor(true), null);
			for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r));
			assert.equal(errors, 1, '30 frames during one failed fetch log it once');
		} finally {
			console.error = realError;
			globalThis.setTimeout = realSetTimeout;
		}
	});

	it('drawWaveRow paints section bands only once the painter has landed', async () => {
		globalThis.Path2D ??= class {
			rect() {}
			moveTo() {}
			lineTo() {}
		};
		const mod = await loadTypeScriptModule(ENTRY);
		const SECTION = '#ff0055';
		const fills = [];
		const ctx = new Proxy(
			{
				fillStyle: '',
				globalAlpha: 1,
				measureText: (t) => ({ width: t.length * 5 }),
				fillRect(x, y, w, h) {
					fills.push(ctx.fillStyle);
				}
			},
			{ get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => ((t[k] = v), true) }
		);
		const sections = { edge: SECTION, rise: SECTION, drop: SECTION, calm: SECTION, body: SECTION, neutral: SECTION, text: '#eee' };
		const palette = {
			bg: '#101010', secondaryBg: '#1a1f28', low: '#e8a13a', mid: '#4f8fff', high: '#f0f0f0',
			tick: '#cccccc', cueHotCue: '#35c04f', cueLoop: '#e8a13a', cueMemory: '#ff3b30',
			cueOutline: '#c8cdd2', phrase: '#888888', vocal: '#99bbff', sections
		};
		const values = Array.from({ length: 64 }, (_, i) => (i % 8) / 8);
		const band = { length: 64, low: values, mid: values, high: values };
		const anlz = {
			stable_id: 'a'.repeat(40),
			points: 64,
			waveform: { kind: 'tri', preview: band, detail: band },
			beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
			cues: [],
			phrases: [{ start_s: 0, end_s: 60, kind: 0, mood: 0, label: 'drop', source: 'own' }],
			vocals: { status: 'not_analyzed' }
		};
		const paint = () => {
			fills.length = 0;
			mod.drawWaveRow(ctx, {
				windowS: 24,
				playheadFrac: 0.5,
				widthCss: 240, heightCss: 60, anlz, positionMs: 20_000, durationMs: 300_000, palette,
				pitch: 1, loop: null, showSections: true
			});
			return fills.filter((s) => s === SECTION).length;
		};
		assert.equal(paint(), 0, 'before the painter lands no band paints (and nothing throws)');
		await mod.loadSectionsApi();
		assert.ok(paint() > 0, 'once the painter lands the same frame paints the section band');
	});
});

describe('the "/" bundle keeps the section painter out', () => {
	const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
	// A VALUE import of the painter module; `import type` is erased at build.
	const STATIC_IMPORT = /^\s*import\s+(?!type\b)[^;]*from\s+'[^']*\/sections';/m;

	it('CONTROL: the probe matches the static import this PR first shipped', () => {
		assert.match("\timport { drawSectionBands, sectionSpans } from '../wave/sections';", STATIC_IMPORT);
		assert.doesNotMatch("import type { SectionSpan } from './sections';", STATIC_IMPORT);
	});

	for (const rel of [
		'wave/render.ts',
		'wave/cues.ts',
		'wave/WaveRow.svelte',
		'deck/strip-waveform-render.ts',
		'deck/StripWaveform.svelte',
		'browser/PreviewStrip.svelte'
	]) {
		it(`${rel} reaches the painter only through sections-lazy`, () => {
			assert.doesNotMatch(read(`../../src/lib/components/rb/${rel}`), STATIC_IMPORT);
		});
	}
});
