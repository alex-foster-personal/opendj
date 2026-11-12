// requirement: PERFMODE-9037
// [if] the Performance tier settings row is on screen [then] it shows On now: Low, On now: Standard, or On now: High plus (Auto), (Override), or (pending host) [else stop]
// [if] the pref is Auto and the resolved tier is STANDARD [then] the row reads On now: Standard (Auto) once the host read has landed, or On now: Standard (pending host) before that [else stop]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const OVERLAY = 'src/lib/components/settings/SettingsOverlay.svelte';

const ENTRY = [
	"export { default as SettingsOverlay } from '$lib/components/settings/SettingsOverlay.svelte';",
	"export { openSettings } from '$lib/settings/overlay.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

const FAKE_NAVIGATION = new URL('./fake-app-navigation.mjs', import.meta.url).pathname;
/** The component reads $page for the setup return path; a static fake page stands in. */
const FAKE_STORES = new URL('./fake-app-stores.mjs', import.meta.url).pathname;

function cards(body) {
	return [...body.matchAll(/<li\b[^>]*class="so-row[^"]*"[^>]*>([\s\S]*?)<\/li>/g)].map((m) => ({
		inner: m[1]
	}));
}

function rowByLabel(rows, label) {
	return rows.find((r) => r.inner.includes(`>${label}</span>`));
}

test('overlay imports tierOnNowLabel and gates the On now span to perf_tier', () => {
	const src = readFileSync(OVERLAY, 'utf8');
	assert.match(src, /import \{ tierOnNowLabel \} from '\$lib\/rb\/perf-tier'/);
	assert.match(src, /def\.id === 'perf_tier'[\s\S]*data-testid="perf-tier-on-now"/);
	assert.equal(
		(src.match(/data-testid="perf-tier-on-now"/g) ?? []).length,
		1,
		'On now testid belongs only on the Performance tier row'
	);
	assert.doesNotMatch(
		src,
		/data-testid="perf-tier-on-now"[\s\S]*def\.id === 'perf_tier'/,
		'the On now span must sit inside the perf_tier gate, not after it'
	);
});

test('SSR Performance tier row shows On now; other enum rows do not', async () => {
	const mod = await loadSvelteSsrModule(ENTRY, {
		alias: { '$app/navigation': FAKE_NAVIGATION, '$app/stores': FAKE_STORES }
	});
	mod.openSettings();
	const body = mod.render(mod.SettingsOverlay).body;
	const rows = cards(body);
	const perf = rowByLabel(rows, 'Performance tier');
	assert.ok(perf, 'Performance tier row rendered');
	assert.match(perf.inner, /data-testid="perf-tier-on-now"/);
	assert.match(perf.inner, /On now: Standard \(pending host\)/);
	const wave = rowByLabel(rows, 'Waveform design');
	assert.ok(wave, 'Waveform design row rendered');
	assert.doesNotMatch(wave.inner, /On now:/);
	assert.doesNotMatch(wave.inner, /data-testid="perf-tier-on-now"/);
});
