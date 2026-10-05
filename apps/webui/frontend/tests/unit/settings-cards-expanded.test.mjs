// requirement: CHROME-17
// [if] the settings overlay opens [then] EVERY card shows its full description with no hover or selection, so nothing expands or moves on hover, [else stop].
// [if] a settings card renders [then] it carries no hover explainer tooltip (the catalog `title` summary) on the card or its control, [else stop].
// [if] the explainer tooltips are removed [then] every control still has an accessible name equal to the setting label, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export { default as SettingsOverlay } from '$lib/components/settings/SettingsOverlay.svelte';",
	"export { openSettings } from '$lib/settings/overlay.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

/** The inert-row tooltip the house rules require; catalog.ts and the parity stubs both declare it unexported. */
const INERT_TITLE = 'not implemented - see PARITY-TODO';

/** SvelteKit's runtime has no build in this harness; the overlay only calls goto on activation. */
const FAKE_NAVIGATION = new URL('./fake-app-navigation.mjs', import.meta.url).pathname;

const escapeHtml = (s) =>
	s.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');

/** Render the open overlay once, server-side: no pointer, no focus, nothing hovered. */
async function renderOpenOverlay() {
	const mod = await loadSvelteSsrModule(ENTRY, { alias: { '$app/navigation': FAKE_NAVIGATION } });
	mod.openSettings();
	return mod.render(mod.SettingsOverlay).body;
}

function cards(body) {
	return [...body.matchAll(/<li\b[^>]*class="so-row[^"]*"[^>]*>([\s\S]*?)<\/li>/g)].map((m) => ({
		open: m[0].slice(0, m[0].indexOf('>') + 1),
		inner: m[1]
	}));
}

test('CHROME-17 if a settings card hides its description until hover then the overlay moves under the pointer', async () => {
	const body = await renderOpenOverlay();
	const rows = cards(body);
	assert.ok(rows.length > 10, `control: the open overlay rendered its cards (got ${rows.length})`);
	const withDetail = rows.filter((r) => /<p class="so-detail[^"]*">[^<]+<\/p>/.test(r.inner));
	assert.equal(withDetail.length, rows.length, `${rows.length - withDetail.length} of ${rows.length} cards render no description without hover`);
});

test('CHROME-17 if a card or its control carries the catalog title as a hover explainer then the explainer tooltips are back', async () => {
	const body = await renderOpenOverlay();
	const { SETTINGS_CATALOG } = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const rows = cards(body);
	for (const r of rows) assert.doesNotMatch(r.open, /\btitle=/, `card carries a hover explainer: ${r.open}`);
	let checked = 0;
	for (const def of SETTINGS_CATALOG) {
		if (def.title === INERT_TITLE) continue; // the inert 'todo' marker keeps its PARITY-TODO tooltip (house rule)
		assert.ok(!body.includes(`title="${escapeHtml(def.title)}"`), `${def.id}: its explainer "${def.title}" is still a hover tooltip`);
		checked += 1;
	}
	assert.ok(checked > 10, 'control: real catalog titles were checked');
});

test('CHROME-17 if dropping the explainers also dropped the accessible names then screen readers lose every control', async () => {
	const body = await renderOpenOverlay();
	const { SETTINGS_CATALOG } = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const named = (def) => body.includes(`aria-label="${escapeHtml(def.label)}"`);
	const controls = SETTINGS_CATALOG.filter((d) => d.implemented && ['enum', 'boolean', 'number', 'multi_bool'].includes(d.control.kind));
	assert.ok(controls.length > 10, 'control: implemented catalog rows exist');
	const rendered = controls.filter((d) => body.includes(`>${escapeHtml(d.label)}</span>`));
	assert.ok(rendered.length > 10, `control: implemented rows rendered in the overlay (got ${rendered.length})`);
	for (const def of rendered) assert.ok(named(def), `${def.id}: control has no aria-label "${def.label}"`);
});
