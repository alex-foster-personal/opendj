// requirement: CHROME-14, CHROME-15
// [if] a new user boots, or an existing user never chose a skin [then] the UI boots in Gothic with the waveform look on Auto, [else stop].
// [if] the waveform prefs are Auto [then] the effective design and palette follow the active skin, live on a skin switch, and an explicit choice overrides the skin, [else stop].
// [if] the Settings preview renders [then] it shows the EFFECTIVE design and palette, never the stored 'auto' token, [else stop].
// [if] a pre-migration blob holds an old default [then] it migrates once to Gothic/Auto, explicit picks are kept, and a migrated blob is never migrated again, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const SRC = 'src/lib';

let skin;
before(async () => {
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
});

afterEach(() => {
	delete globalThis.window;
	delete globalThis.document;
});

/** Fake browser globals so prefs.svelte.ts persists and applies prefs like it does in the app. */
function installBrowser(store = new Map()) {
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => void store.set(k, String(v)),
			removeItem: (k) => void store.delete(k)
		}
	};
	globalThis.document = { documentElement: { dataset: {}, style: {} } };
	return store;
}

const loadPrefs = () => loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');

/** A full blob as a PREVIOUS build wrote it: written by the real setters, then
 * rewritten to the pre-migration shape (no defaults_migration) with `fields`. */
async function preMigrationBlob(fields) {
	const store = installBrowser();
	const first = await loadPrefs();
	first.setUiSkin('light'); // any write, so the whole blob lands in storage
	const blob = JSON.parse(store.get(STORAGE_KEY));
	delete blob.defaults_migration;
	for (const [key, value] of Object.entries(fields)) {
		if (value === undefined) delete blob[key];
		else blob[key] = value;
	}
	store.set(STORAGE_KEY, JSON.stringify(blob));
	return store;
}

// ------------------------------------------------------------ resolution

test("CHROME-15 if Auto does not resolve to each skin's declared look then switching skin cannot change the waveforms", () => {
	assert.deepEqual(skin.SKIN_WAVE_LOOK, {
		default: { design: 'tri-band', palette: 'rekordbox' },
		'mono-dev': { design: 'blocks', palette: 'mono' },
		light: { design: 'tri-band', palette: 'rekordbox' }
	});
	for (const s of skin.UI_SKIN_CHOICES) {
		assert.equal(skin.effectiveWaveformDesign('auto', s), skin.SKIN_WAVE_LOOK[s].design, s);
		assert.equal(skin.effectiveWavePalette('auto', s), skin.SKIN_WAVE_LOOK[s].palette, s);
	}
});

test('CHROME-15 if an explicit design or palette is overridden by the skin then a user choice is lost', () => {
	for (const s of skin.UI_SKIN_CHOICES) {
		assert.equal(skin.effectiveWaveformDesign('line', s), 'line');
		assert.equal(skin.effectiveWaveformDesign('tri-band', s), 'tri-band');
		assert.equal(skin.effectiveWavePalette('legacy', s), 'legacy');
		assert.equal(skin.effectiveWavePalette('rekordbox', s), 'rekordbox');
	}
});

// ------------------------------------------------------------ first run + live switch

test('CHROME-14 if a first run does not boot Gothic with both waveform prefs on Auto then the advertised UI is not what users get', async () => {
	const store = installBrowser();
	const prefs = await loadPrefs();
	assert.equal(prefs.uiPrefs.ui_skin, 'mono-dev');
	assert.equal(prefs.uiPrefs.waveform_design, 'auto');
	assert.equal(prefs.uiPrefs.wave_palette, 'auto');
	assert.equal(globalThis.document.documentElement.dataset.skin, 'mono-dev');
	assert.equal(globalThis.document.documentElement.dataset.wavePalette, 'mono', 'Auto under Gothic paints mono');
	assert.equal(store.has(STORAGE_KEY), false, 'control: a first run writes nothing until a pref changes');
});

test('CHROME-15 if switching skin with Auto does not swap the painted palette then Gothic keeps the old colors', async () => {
	installBrowser();
	const prefs = await loadPrefs();
	const root = globalThis.document.documentElement;
	assert.equal(root.dataset.wavePalette, 'mono');
	prefs.setUiSkin('default');
	assert.equal(root.dataset.wavePalette, undefined, 'Default skin + Auto = rekordbox (no attribute)');
	prefs.cycleUiSkin(); // default -> mono-dev
	assert.equal(root.dataset.wavePalette, 'mono');
	prefs.setWavePalette('legacy');
	prefs.cycleUiSkin(); // mono-dev -> light, explicit legacy must stay
	assert.equal(root.dataset.wavePalette, 'legacy');
	assert.equal(prefs.uiPrefs.wave_palette, 'legacy');
});

// ------------------------------------------------------------ settings preview (real SSR render)

const PREVIEW_ENTRY = [
	"export { default as Preview } from '$lib/components/settings/WaveformDesignPreview.svelte';",
	"export * as prefs from '$lib/rb/prefs.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

function previewAttrs(mod) {
	const body = mod.render(mod.Preview).body;
	const design = body.match(/data-design="([^"]*)"/)?.[1];
	const palette = body.match(/data-palette="([^"]*)"/)?.[1];
	assert.ok(design !== undefined && palette !== undefined, `preview rendered no look attributes: ${body}`);
	return { design, palette };
}

test('CHROME-15 if the Settings preview shows the stored token instead of the effective look then switching to Gothic does not change it', async () => {
	installBrowser();
	const mod = await loadSvelteSsrModule(PREVIEW_ENTRY);
	assert.equal(mod.prefs.uiPrefs.waveform_design, 'auto', 'precondition: stored token is auto');
	assert.deepEqual(previewAttrs(mod), { design: 'blocks', palette: 'mono' });
	mod.prefs.setUiSkin('default');
	assert.deepEqual(previewAttrs(mod), { design: 'tri-band', palette: 'rekordbox' });
	mod.prefs.setUiSkin('mono-dev');
	mod.prefs.setWaveformDesign('line');
	mod.prefs.setWavePalette('legacy');
	assert.deepEqual(previewAttrs(mod), { design: 'line', palette: 'legacy' }, 'explicit choices win over Gothic');
});

test('CHROME-15 if a painter reads the stored waveform token instead of the effective one then Auto paints nothing the skin declared', () => {
	const painters = [
		'components/rb/wave/WaveRow.svelte',
		'components/rb/deck/StripWaveform.svelte',
		'components/rb/browser/PreviewStrip.svelte',
		'components/settings/WaveformDesignPreview.svelte'
	];
	for (const file of painters) {
		const src = readFileSync(`${SRC}/${file}`, 'utf8');
		const raw = src.match(/uiPrefs\.waveform_design\b/g)?.length ?? 0;
		const resolved = src.match(/effectiveWaveformDesign\(uiPrefs\.waveform_design, uiPrefs\.ui_skin\)/g)?.length ?? 0;
		assert.ok(raw > 0, `${file}: control, the painter reads the design pref`);
		assert.equal(raw, resolved, `${file}: every waveform_design read goes through effectiveWaveformDesign`);
		assert.doesNotMatch(src, /resolveStripBandColors\([^)]*uiPrefs\.wave_palette\)/, `${file}: palette must be resolved`);
	}
});

test('CHROME-15 if Settings offers no Auto option, or Auto is not first, then the default cannot be re-picked', async () => {
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const defs = catalog.SETTINGS_CATALOG;
	assert.ok(Array.isArray(defs) && defs.length > 10, 'control: the catalog loaded');
	for (const id of ['waveform_design', 'wave_palette']) {
		const def = defs.find((d) => d.id === id);
		assert.ok(def, `${id} missing from the settings catalog`);
		assert.deepEqual(def.control.options[0], { value: 'auto', label: 'Auto (follows skin)' });
	}
});

// ------------------------------------------------------------ migration

test('CHROME-14 if an unmigrated blob holding the old defaults does not move to Gothic and Auto then existing users never see the new default', async () => {
	const store = await preMigrationBlob({ ui_skin: 'default', waveform_design: 'tri-band', wave_palette: 'rekordbox' });
	installBrowser(store);
	const prefs = await loadPrefs();
	assert.equal(prefs.uiPrefs.ui_skin, 'mono-dev');
	assert.equal(prefs.uiPrefs.waveform_design, 'auto');
	assert.equal(prefs.uiPrefs.wave_palette, 'auto');
	const saved = JSON.parse(store.get(STORAGE_KEY));
	assert.equal(saved.defaults_migration, prefs.PREFS_DEFAULTS_MIGRATION, 'boot persists the migration marker');
	assert.equal(saved.ui_skin, 'mono-dev');
});

test('CHROME-14 if a blob from before the skin keys existed does not boot Gothic and Auto then upgraders keep the old look', async () => {
	const store = await preMigrationBlob({ ui_skin: undefined, waveform_design: undefined, wave_palette: undefined });
	installBrowser(store);
	const prefs = await loadPrefs();
	assert.deepEqual(
		[prefs.uiPrefs.ui_skin, prefs.uiPrefs.waveform_design, prefs.uiPrefs.wave_palette],
		['mono-dev', 'auto', 'auto']
	);
});

test('CHROME-14 if the migration overwrites an explicit non-default choice then a user who picked a look loses it', async () => {
	const store = await preMigrationBlob({ ui_skin: 'light', waveform_design: 'line', wave_palette: 'legacy' });
	installBrowser(store);
	const prefs = await loadPrefs();
	assert.deepEqual(
		[prefs.uiPrefs.ui_skin, prefs.uiPrefs.waveform_design, prefs.uiPrefs.wave_palette],
		['light', 'line', 'legacy']
	);
});

test('CHROME-14 if a migrated blob is migrated again then a later explicit pick of Default never sticks', async () => {
	const store = installBrowser();
	const first = await loadPrefs();
	first.setUiSkin('default');
	first.setWaveformDesign('tri-band');
	first.setWavePalette('rekordbox');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).defaults_migration, 1);
	installBrowser(store);
	const prefs = await loadPrefs();
	assert.deepEqual(
		[prefs.uiPrefs.ui_skin, prefs.uiPrefs.waveform_design, prefs.uiPrefs.wave_palette],
		['default', 'tri-band', 'rekordbox']
	);
});

test('CHROME-14 if a junk migration marker loads silently then a corrupt blob is indistinguishable from an old one', async () => {
	const store = await preMigrationBlob({ defaults_migration: 'yes' });
	installBrowser(store);
	await assert.rejects(loadPrefs(), /defaults_migration must be a non-negative integer/);
});
