// requirement: WAVEQS-11736
// [if] the bar lines row stays inert, its title omits the CLI verb, or the CLI drops a grid value [then] this fails, [else stop].
// requirement: CUSTSCR-10464
// [if] the popover lacks a number row branch with SettingNumberStepper [then] this fails, [else stop].
// requirement: WAVEQS-11730
// [if] hot, memory or loop marker rows stay inert or lack a verb [then] this fails, [else stop].
// requirement: WAVEQS-8348b
// [if] a quick-settings row names no Settings catalog entry, or one the Settings path refuses [then] this fails, [else stop].
// [if] an inert row carries any title but the PARITY-TODO one [then] this fails, [else stop].
// [if] the popover or Settings renders a preview other than through SettingPreview [then] this fails, [else stop].
// [if] a live row has no command-bus verb, or the overlay verb domain drifts from the CLI [then] this fails, [else stop].
// [if] the glyph shows while the daemon has not declared waveform.quick_settings on [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { compile } from 'svelte/compiler';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { bundleWaveQuickSettingsClient } from './mount-svelte-client.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';

let qs;
let catalog;
let flag;
let overlay;

before(async () => {
	qs = await loadTypeScriptModule('src/lib/settings/waveform-quick-settings.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	flag = await loadTypeScriptModule('src/lib/rb/waveform-quick-settings-flag.ts');
	overlay = await loadTypeScriptModule('src/lib/rb/waveform-overlay-command.ts');
});

/** Every live row's setting id, in display order. */
function settingIds() {
	return qs.WAVE_QS_GROUPS.flatMap((g) => g.rows.filter((r) => r.kind === 'setting').map((r) => r.id));
}

/** The ui-prefs key each set_waveform_overlay overlay must write (the Settings row it twins). */
const OVERLAY_PREF = {
	sections: 'show_sections',
	lyrics: 'lyrics_waveform_overlay',
	lyrics_scrub: 'lyrics_hover_scrub',
	jog_radial: 'jog_radial_waveform',
	hot_cues: 'show_hot_cue_markers',
	memory_cues: 'show_memory_cue_markers',
	loop_cues: 'show_loop_markers',
	vocal_lane: 'show_vocal_lane',
	grid_lines: 'wave_grid_lines'
};

/** ALLOWED_SETTING_KEYS read out of apply.ts source (the module itself needs the rune singleton). */
function allowedSettingKeys() {
	const src = readFileSync('src/lib/settings/apply.ts', 'utf8');
	const block = src.match(/export const ALLOWED_SETTING_KEYS = \[([\s\S]*?)\] as const;/);
	assert.ok(block, 'could not read ALLOWED_SETTING_KEYS out of apply.ts');
	return new Set([...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1]));
}

test('WAVEQS-8348b every live row resolves to an implemented boolean or enum catalog entry the Settings path accepts', () => {
	const ids = settingIds();
	assert.ok(ids.length >= 10, `expected the full option set, got ${ids.length}`);
	const allowed = allowedSettingKeys();
	for (const id of ids) {
		const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, id);
		assert.equal(def.id, id);
		assert.ok(allowed.has(id), `${id} is not an applySettingChange key`);
	}
	// The two rows Settings shows a live preview for carry it here too.
	const previewed = ids.filter((id) => qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, id).preview === 'waveform');
	assert.deepEqual(previewed.sort(), ['wave_grid_lines', 'wave_height', 'wave_palette', 'waveform_design']);
});

test('WAVEQS-11733 the half or full height row is live and names opendj set_waveform_design', () => {
	const look = qs.WAVE_QS_GROUPS.find((g) => g.id === 'look');
	assert.ok(look);
	assert.ok(look.rows.some((r) => r.kind === 'setting' && r.id === 'wave_height'));
	assert.ok(!look.rows.some((r) => r.id === 'wave_height' && r.kind === 'inert'));
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'wave_height');
	assert.equal(def.control.kind, 'enum');
	assert.deepEqual(
		def.control.options.map((o) => o.value),
		['full', 'half']
	);
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_design$/);
	const allowed = allowedSettingKeys();
	assert.ok(allowed.has('wave_height'));
});

test('WAVEQS-8348b resolve refuses a missing or unimplemented entry (negative controls)', () => {
	assert.throws(() => qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'no_such_setting'), /no Settings catalog entry/);
	const fake = [{ id: 'x', implemented: false, control: { kind: 'boolean' } }];
	assert.throws(() => qs.resolveWaveQsSetting(fake, 'x'), /not implemented/);
	const link = [{ id: 'y', implemented: true, control: { kind: 'link', href: '/' } }];
	assert.throws(() => qs.resolveWaveQsSetting(link, 'y'), /only boolean, enum and number/);
	const numberFake = [
		{
			id: 'n',
			label: 'N',
			title: 'N',
			detail: 'N',
			implemented: true,
			control: { kind: 'number', min: 0, max: 100, step: 5, defaultValue: 50, unit: ' %' }
		}
	];
	assert.equal(qs.resolveWaveQsSetting(numberFake, 'n').id, 'n');
});

test('CUSTSCR-10464 popover renders number rows through SettingNumberStepper', () => {
	const popover = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(popover, /import SettingNumberStepper from/);
	assert.match(popover, /\{:else if def\.control\.kind === 'number'\}/);
	assert.match(popover, /applySettingChange\(def\.id, String\(next\)\)/);
});

test('WAVEQS-8348b inert rows carry exactly the PARITY-TODO title and every group is non-empty', () => {
	assert.equal(qs.WAVE_QS_INERT_TITLE, 'not implemented - see PARITY-TODO');
	const inert = qs.WAVE_QS_GROUPS.flatMap((g) => g.rows.filter((r) => r.kind === 'inert'));
	assert.ok(inert.some((r) => r.id === 'tempo_lane'), 'the tempo-change lane row is reserved');
	const tempoDisplay = qs.resolveWaveQsInertDisplay(catalog.SETTINGS_CATALOG, 'tempo_lane');
	// WAVEQS-10121: the reserved row points at the shipped Settings controls, never claims the lane is unbuilt.
	assert.doesNotMatch(tempoDisplay.detail, /being built|own branch|not built/i);
	const tempoCatalog = catalog.SETTINGS_CATALOG.filter((d) => d.id === 'show_tempo_lane' || d.id === 'tempo_lane_scale');
	assert.equal(tempoCatalog.length, 2, 'both tempo-lane settings exist in the catalog');
	for (const def of tempoCatalog) assert.ok(tempoDisplay.detail.includes(def.label), `the row names ${def.label}`);
	for (const group of qs.WAVE_QS_GROUPS) assert.ok(group.rows.length > 0, `${group.id} is empty`);
	const popover = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(popover, /title=\{WAVE_QS_INERT_TITLE\}/);
	assert.match(popover, /\sdisabled\s+aria-label=\{display\.label\}/);
});

test('WAVEQS-8348b issue #12518 every row label and detail comes from the catalog resolver', () => {
	for (const id of settingIds()) {
		const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, id);
		const catalogDef = catalog.SETTINGS_CATALOG.find((d) => d.id === id);
		assert.ok(catalogDef, `${id} missing from SETTINGS_CATALOG`);
		assert.equal(def.label, catalogDef.label);
		assert.equal(def.detail, catalogDef.detail);
	}
	for (const row of qs.WAVE_QS_GROUPS.flatMap((g) => g.rows.filter((r) => r.kind === 'inert'))) {
		const display = qs.resolveWaveQsInertDisplay(catalog.SETTINGS_CATALOG, row.id);
		if (row.id === 'phrase_markers') {
			const stub = qs.WAVE_QS_INERT_DEFS.find((d) => d.id === 'phrase_markers');
			assert.ok(stub);
			assert.equal(display.label, stub.label);
			assert.equal(display.detail, stub.detail);
		}
		assert.ok(display.label.length > 0);
		assert.ok(display.detail.length > 0);
	}
});

test('WAVEQS-8348c Settings and the popover both render previews through the one SettingPreview component', () => {
	const shared = readFileSync('src/lib/components/settings/SettingPreview.svelte', 'utf8');
	assert.match(shared, /import WaveformDesignPreview from '\.\/WaveformDesignPreview\.svelte';/);
	for (const file of ['src/lib/components/settings/SettingsOverlay.svelte', 'src/lib/components/rb/WaveQuickSettingsPopover.svelte']) {
		const src = readFileSync(file, 'utf8');
		assert.match(src, /import SettingPreview from '[^']*SettingPreview\.svelte';/, `${file} must import SettingPreview`);
		assert.match(src, /<SettingPreview kind=\{def\.preview\} label=\{`\$\{def\.label\} preview`\} \/>/, `${file} must render the row's declared preview, named by its row`);
		assert.doesNotMatch(src, /WaveformDesignPreview/, `${file} must not render the painter directly`);
	}
});

test('WAVEQS-8348c the popover writes through applySettingChange and reads through readSettingValue, never uiPrefs directly', () => {
	const src = readFileSync('src/lib/components/rb/WaveQuickSettingsPopover.svelte', 'utf8');
	assert.match(src, /applySettingChange\(def\.id/);
	assert.match(src, /readSettingValue\(def\.id\)/);
	assert.doesNotMatch(src, /uiPrefs/);
});

test('WAVEQS-8348e every live row has a command-bus verb that exists in the CLI catalog', () => {
	const cli = readFileSync('../../opendj_cli/catalog.py', 'utf8');
	for (const id of settingIds()) {
		const verb = qs.WAVE_QS_VERBS[id];
		assert.ok(verb, `${id} has no verb`);
		assert.match(
			cli,
			new RegExp(`Verb\\(\\s*"${verb}"\\s*,\\s*"${verb}"`),
			`${verb} is not in apps/opendj_cli/catalog.py`
		);
	}
	const values = cli.match(/WAVEFORM_OVERLAY_VALUES: tuple\[str, \.\.\.\] = \(([^)]*)\)/);
	assert.ok(values, 'WAVEFORM_OVERLAY_VALUES missing from the CLI catalog');
	assert.deepEqual([...values[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]), [...overlay.WAVEFORM_OVERLAYS]);
	for (const name of overlay.WAVEFORM_OVERLAYS) {
		assert.equal(qs.WAVE_QS_VERBS[OVERLAY_PREF[name]], 'set_waveform_overlay');
	}
	assert.throws(() => overlay.parseWaveformOverlay('beats'), /overlay must be/);
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'show_sections');
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_overlay$/);
	assert.throws(() => qs.waveQsRowTitle({ ...def, id: 'unmapped' }), /no command-bus verb/);
});

test('WAVEQS-8348 the glyph shows only when the daemon declares waveform.quick_settings on', () => {
	const full = JSON.parse(readFileSync('tests/unit/fixtures/flags-full.json', 'utf8'));
	assert.equal(flag.waveQsEnabledIn(full.flags), true);
	assert.equal(flag.waveQsEnabledIn(full.flags.filter((f) => f.flag_id !== 'waveform.quick_settings')), false);
	assert.equal(flag.waveQsEnabledIn([{ flag_id: 'waveform.quick_settings', enabled: false }]), false);
	assert.equal(flag.waveQsEnabledIn([]), false, 'flags still loading show no glyph');
});

// requirement: WAVEQS-11734
// [if] show_vocal_lane is inert or missing from overlays [then] this fails, [else stop].
test('WAVEQS-11734 show_vocal_lane is a live markers row with set_waveform_overlay vocal_lane parity', () => {
	const markers = qs.WAVE_QS_GROUPS.find((g) => g.id === 'markers');
	assert.ok(markers);
	const row = markers.rows.find((r) => r.kind === 'setting' && r.id === 'show_vocal_lane');
	assert.ok(row, 'show_vocal_lane must be a live setting row in markers');
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'show_vocal_lane');
	assert.equal(def.control.kind, 'boolean');
	assert.equal(def.group, 'performance');
	assert.equal(def.implemented, true);
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_overlay$/);
	const inertVocal = qs.WAVE_QS_GROUPS.flatMap((g) => g.rows).find((r) => r.kind === 'inert' && r.id === 'vocal_lane');
	assert.equal(inertVocal, undefined, 'vocal_lane must not remain an inert row');
});

test('WAVEQS-8348 the glyph title is the requirement text verbatim', () => {
	const button = readFileSync('src/lib/components/rb/WaveQuickSettingsButton.svelte', 'utf8');
	assert.match(button, /const TITLE = 'Waveform quick settings';/);
	assert.match(button, /title=\{TITLE\}/);
	assert.doesNotMatch(button, /loadError/, 'a load error must not replace the title');
});

test('WAVEQS-11736 the bar lines row is live, its title names the CLI verb, and the CLI declares the three values', async () => {
	const grid = await loadTypeScriptModule('src/lib/rb/wave-grid-lines.ts');
	const rows = qs.WAVE_QS_GROUPS.flatMap((g) => g.rows);
	assert.ok(!rows.some((r) => r.id === 'bar_lines'), 'the inert bar_lines row is gone');
	const markers = qs.WAVE_QS_GROUPS.find((g) => g.id === 'markers');
	assert.ok(markers.rows.some((r) => r.kind === 'setting' && r.id === 'wave_grid_lines'));
	const def = qs.resolveWaveQsSetting(catalog.SETTINGS_CATALOG, 'wave_grid_lines');
	assert.match(qs.waveQsRowTitle(def), /Agents: opendj set_waveform_overlay$/);
	assert.equal(def.control.kind, 'enum');
	assert.deepEqual(def.control.options.map((o) => o.value), ['beats_bars', 'bars', 'off']);
	const cli = readFileSync('../../opendj_cli/catalog.py', 'utf8');
	const values = cli.match(/WAVE_GRID_LINES_VALUES: tuple\[str, \.\.\.\] = \(([^)]*)\)/);
	assert.ok(values, 'WAVE_GRID_LINES_VALUES missing from the CLI catalog');
	assert.deepEqual([...values[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]), [...grid.WAVE_GRID_LINES_VALUES]);
});

// ---- issue #12519: one shared read and write path, at runtime ----
// requirement: WAVEQS-8348c
// [if] a popover row change is not visible through the Settings read path, is not sent as PUT /api/v1/ui-prefs, or is lost on reload [then] this fails, [else stop].

const WAVEQS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

/** Install a fake window, document and fetch; return the localStorage map and the recorded ui-prefs PUT bodies. */
function _installSharedPathEnv(store) {
	const puts = [];
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v),
			removeItem: (k) => store.delete(k)
		},
		matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
	};
	globalThis.localStorage = globalThis.window.localStorage;
	globalThis.document = { documentElement: { dataset: {}, style: { setProperty() {} } } };
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		if (request.method === 'PUT' && new URL(request.url).pathname === '/api/v1/ui-prefs') {
			puts.push(await request.clone().json());
		}
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
	return puts;
}

/** Load the popover and Settings shared modules as one fresh app boot (a page reload). */
async function _bootSharedPath() {
	const { loadRuneModule } = await import('./load-rune-module.mjs');
	return loadRuneModule(
		[
			"export { applySettingChange, readSettingValue } from '$lib/settings/apply';",
			"export { WAVE_QS_GROUPS, resolveWaveQsSetting } from '$lib/settings/waveform-quick-settings';",
			"export { SETTINGS_CATALOG } from '$lib/settings/catalog';"
		].join('\n')
	);
}

/** A value for `def` that differs from `current`: the flipped boolean or another enum option. */
function _otherValue(def, current) {
	if (def.control.kind === 'boolean') return current !== true;
	return def.control.options.map((o) => o.value).find((v) => v !== current);
}

/** Let the queued disk write chain drain. */
async function _settleWrites() {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

test('WAVEQS-8348c a popover row change reads back through the Settings path, PUTs /api/v1/ui-prefs and survives reload', async () => {
	const saved = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch, localStorage: globalThis.localStorage };
	try {
		const store = new Map();
		const puts = _installSharedPathEnv(store);
		const app = await _bootSharedPath();
		const expected = new Map();
		for (const group of app.WAVE_QS_GROUPS) {
			for (const row of group.rows) {
				if (row.kind !== 'setting') continue;
				const def = app.resolveWaveQsSetting(app.SETTINGS_CATALOG, row.id);
				if (def.control.kind !== 'boolean' && def.control.kind !== 'enum') continue;
				const next = _otherValue(def, app.readSettingValue(def.id));
				app.applySettingChange(def.id, next);
				assert.equal(app.readSettingValue(def.id), next, `${def.id}: Settings must see the popover change at once`);
				expected.set(def.id, next);
			}
		}
		assert.ok(expected.size >= 10, `expected the live waveform rows, got ${expected.size}`);
		await _settleWrites();
		const putKeys = new Set(puts.flatMap((body) => Object.keys(body)));
		for (const id of ['wave_height', 'show_vocal_lane', 'show_hot_cue_markers', 'wave_grid_lines']) {
			assert.ok(expected.has(id), `${id} must be a live popover row`);
			assert.ok(putKeys.has(id), `${id} must persist through PUT /api/v1/ui-prefs`);
		}
		assert.ok(store.has(WAVEQS_STORAGE_KEY), 'the change must land in the ui-prefs blob');
		_installSharedPathEnv(store);
		const reloaded = await _bootSharedPath();
		for (const [id, value] of expected) {
			assert.equal(reloaded.readSettingValue(id), value, `${id} must survive a reload`);
		}
	} finally {
		globalThis.window = saved.window;
		globalThis.document = saved.document;
		globalThis.fetch = saved.fetch;
		globalThis.localStorage = saved.localStorage;
	}
});

test('WAVEQS-8348c the Settings overlay reads and writes through the same apply.ts pair as the popover', () => {
	const src = readFileSync('src/lib/components/settings/SettingsOverlay.svelte', 'utf8');
	assert.match(src, /import \{[^}]*\bapplySettingChange\b[^}]*\breadSettingValue\b[^}]*\} from '\$lib\/settings\/apply';/);
	assert.match(src, /applySettingChange\(def\.id, /);
	assert.match(src, /readSettingValue\(def\.id as AllowedSettingKey\)/);
});

// ----- WAVEQS-8348d keyboard dialog: the real glyph and popover, client-mounted on the DOM shim -----
// requirement: WAVEQS-8348d, WAVEQS-14462

let client;
let teardownDom = null;
const windowListeners = [];

/** One compound selector (tag, .class, #id, [attr], [attr="v"], :disabled, :not(...)) against a shim element. */
function matchesCompound(el, compound) {
	let rest = compound;
	const tag = rest.match(/^[a-z][\w-]*/i);
	if (tag) {
		if (el.tagName.toLowerCase() !== tag[0].toLowerCase()) return false;
		rest = rest.slice(tag[0].length);
	}
	while (rest) {
		let m;
		if ((m = rest.match(/^:not\(([^)]*)\)/))) {
			if (matchesCompound(el, m[1])) return false;
		} else if ((m = rest.match(/^:disabled/))) {
			if (!(el.disabled === true || el.hasAttribute('disabled'))) return false;
		} else if ((m = rest.match(/^\.([\w-]+)/))) {
			if (!el.classList.contains(m[1])) return false;
		} else if ((m = rest.match(/^#([\w-]+)/))) {
			if (el.getAttribute('id') !== m[1]) return false;
		} else if ((m = rest.match(/^\[([\w-]+)(?:="([^"]*)")?\]/))) {
			const value = el.getAttribute(m[1]);
			if (value === null || (m[2] !== undefined && value !== m[2])) return false;
		} else {
			throw new Error(`test selector engine: unsupported selector part ${rest}`);
		}
		rest = rest.slice(m[0].length);
	}
	return true;
}

/** Selector lists with descendant combinators, enough for focus-trap.ts and the popover. */
function matchesSelector(el, selector) {
	return selector.split(',').some((part) => {
		const chain = part.trim().split(/\s+/);
		if (!matchesCompound(el, chain[chain.length - 1])) return false;
		let ancestor = el.parentElement;
		for (let i = chain.length - 2; i >= 0; i -= 1) {
			while (ancestor && !matchesCompound(ancestor, chain[i])) ancestor = ancestor.parentElement;
			if (!ancestor) return false;
			ancestor = ancestor.parentElement;
		}
		return true;
	});
}

const NATIVE_TAB_STOPS = new Set(['a', 'button', 'input', 'select', 'textarea']);

/** Give the quality-badge shim the focus, selector and layout surface the dialog needs. */
function extendShim() {
	const proto = globalThis.Element.prototype;
	proto.matches = function (selector) {
		return matchesSelector(this, selector);
	};
	proto.closest = function (selector) {
		for (let el = this; el; el = el.parentElement) if (matchesSelector(el, selector)) return el;
		return null;
	};
	proto.focus = function () {
		this.ownerDocument.activeElement = this;
	};
	proto.blur = function () {
		if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
	};
	Object.defineProperty(proto, 'tabIndex', {
		configurable: true,
		get() {
			const attr = this.getAttribute('tabindex');
			if (attr !== null) return Number(attr);
			return NATIVE_TAB_STOPS.has(this.tagName.toLowerCase()) ? 0 : -1;
		},
		set(v) {
			this.setAttribute('tabindex', String(v));
		}
	});
	Object.defineProperty(proto, 'options', {
		configurable: true,
		get() {
			return this.querySelectorAll('option');
		}
	});
	Object.defineProperty(proto, 'offsetWidth', { configurable: true, get: () => 600 });
	Object.defineProperty(proto, 'offsetHeight', { configurable: true, get: () => 400 });
	proto.checkVisibility = () => true;
	proto.getBoundingClientRect = () => ({ left: 900, right: 920, top: 10, bottom: 26, width: 20, height: 16 });
	proto.getClientRects = () => [{ width: 1 }];
	globalThis.MutationObserver = class {
		observe() {}
		disconnect() {}
	};
	globalThis.innerWidth = 1440;
	globalThis.innerHeight = 900;
	globalThis.addEventListener = (type, fn, options) => windowListeners.push({ type, fn, options });
	globalThis.removeEventListener = (type, fn, options) => {
		const capture = (o) => (typeof o === 'boolean' ? o : o?.capture === true);
		const i = windowListeners.findIndex((l) => l.type === type && l.fn === fn && capture(l.options) === capture(options));
		if (i !== -1) windowListeners.splice(i, 1);
	};
}

before(async () => {
	teardownDom = installQualityBadgeDom();
	extendShim();
	client = await bundleWaveQuickSettingsClient();
});

after(() => {
	for (const key of ['addEventListener', 'removeEventListener', 'MutationObserver', 'innerWidth', 'innerHeight'])
		delete globalThis[key];
	teardownDom?.();
});

function domEvent(type, init = {}) {
	return {
		type,
		...init,
		defaultPrevented: false,
		cancelBubble: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.cancelBubble = true;
		}
	};
}

/** Window capture listeners first, then the target's own path, the order a browser dispatches in. */
function fire(target, type, init = {}) {
	const event = domEvent(type, { target, ...init });
	for (const l of [...windowListeners]) if (l.type === type && !event.cancelBubble) l.fn(event);
	if (!event.cancelBubble) target.dispatchEvent(event);
	return event;
}

async function settle() {
	for (let i = 0; i < 6; i += 1) {
		await new Promise((resolve) => setTimeout(resolve, 0));
		client.flushSync();
	}
}

/** A host and an outside sibling under document.body, both removed when the test ends. */
function stage(t) {
	const host = document.createElement('div');
	const outside = document.createElement('div');
	document.body.appendChild(host);
	document.body.appendChild(outside);
	t.after(() => {
		host.remove();
		outside.remove();
	});
	return { host, outside };
}

/** Mount the real glyph, focus it as a click does, click it, and wait for the lazy popover. */
async function openFromGlyph(t) {
	const { host } = stage(t);
	const instance = client.mount(client.WaveQuickSettingsButton, { target: host });
	t.after(() => client.unmount(instance));
	client.flushSync();
	const glyph = host.querySelector('[data-testid="wave-quick-settings-button"]');
	assert.ok(glyph, 'the glyph did not render');
	assert.ok(document.querySelector('[role="dialog"]') === null, 'the dialog rendered before the first click');
	glyph.focus();
	fire(glyph, 'click');
	await settle();
	const dialog = host.querySelector('[role="dialog"]');
	assert.ok(dialog, 'a glyph click did not open the dialog');
	return { glyph, dialog };
}

/**
 * Mount the real popover against a focused stand-in glyph, as the glyph does; `close()` unmounts it the
 * way the glyph's {#if} does. (The DOM shim does not detach a closed {#if} branch, so close paths are
 * asserted on the popover's onclose contract plus the unmount.)
 */
async function openPopover(t) {
	const { host, outside } = stage(t);
	const glyph = document.createElement('button');
	host.appendChild(glyph);
	glyph.focus();
	const closes = [];
	let instance = client.mount(client.WaveQuickSettingsPopover, {
		target: host,
		props: { anchor: glyph, onclose: () => closes.push(document.activeElement) }
	});
	const close = () => {
		if (instance === null) return;
		client.unmount(instance);
		instance = null;
		client.flushSync();
	};
	t.after(close);
	await settle();
	const dialog = host.querySelector('[role="dialog"]');
	assert.ok(dialog, 'the popover rendered no dialog');
	return { glyph, dialog, outside, closes, close };
}

function firstOption(dialog) {
	return dialog.querySelector('.wqs-group select, .wqs-group input:not(:disabled)');
}

function tabStops(dialog) {
	return dialog
		.querySelectorAll('button, input, select')
		.filter((el) => !matchesSelector(el, ':disabled') && el.tabIndex >= 0);
}

test('WAVEQS-8348d the glyph opens a labeled dialog with focus on its first option control', async (t) => {
	// [if] the glyph click opens no labeled dialog or focus lands off the first option [then] the dialog is not keyboard-ready, [else stop].
	const { glyph, dialog } = await openFromGlyph(t);
	assert.equal(dialog.getAttribute('aria-label'), 'Waveform quick settings');
	assert.equal(glyph.getAttribute('aria-expanded'), 'true');
	const first = firstOption(dialog);
	assert.ok(first, 'the dialog has no option control');
	assert.ok(document.activeElement === first, 'focus is not on the first option control');
	assert.ok(document.activeElement !== dialog.querySelector('.wqs-all'), 'focus went to All settings');
});

test('WAVEQS-8348d Tab and Shift+Tab reach every dialog control and stay inside it', async (t) => {
	// [if] Tab skips a dialog control or walks focus out of the open dialog [then] keyboard users are stranded, [else stop].
	const { dialog } = await openPopover(t);
	const stops = tabStops(dialog);
	assert.ok(stops.length >= 10, `expected every option control, got ${stops.length}`);
	assert.ok(stops.includes(dialog.querySelector('.wqs-all')), 'All settings is not a Tab stop');
	const visited = new Set();
	for (let i = 0; i < stops.length; i += 1) {
		const event = fire(document.activeElement, 'keydown', { key: 'Tab', shiftKey: false });
		assert.equal(event.defaultPrevented, true, 'Tab was not handled inside the dialog');
		assert.ok(dialog.contains(document.activeElement), `Tab ${i + 1} left the dialog`);
		visited.add(document.activeElement);
	}
	const missed = stops.filter((el) => !visited.has(el)).map((el) => el.id || el.className);
	assert.deepEqual(missed, [], 'some controls were never reached by Tab');
	assert.ok(document.activeElement === firstOption(dialog), 'Tab did not wrap back to the first option');
	fire(document.activeElement, 'keydown', { key: 'Tab', shiftKey: true });
	assert.ok(dialog.contains(document.activeElement), 'Shift+Tab left the dialog');
	assert.ok(document.activeElement === dialog.querySelector('.wqs-all'), 'Shift+Tab did not step back to All settings');
});

test('WAVEQS-8348d Escape closes the dialog and returns focus to the glyph', async (t) => {
	// [if] Escape leaves the dialog open or focus does not return to the glyph [then] it traps the user, [else stop].
	const { glyph, closes, close } = await openPopover(t);
	const event = fire(document.activeElement, 'keydown', { key: 'Escape' });
	assert.equal(event.defaultPrevented, true, 'Escape was not consumed by the dialog');
	assert.equal(closes.length, 1, 'Escape did not close the dialog');
	assert.ok(document.activeElement === glyph, 'Escape did not return focus to the glyph');
	close();
	assert.ok(document.activeElement === glyph, 'unmount moved focus off the glyph');
});

test('WAVEQS-8348d a pointer press outside closes the dialog, one inside keeps it open', async (t) => {
	// [if] an outside press leaves the dialog open or an inside press closes it [then] pointer dismissal is broken, [else stop].
	const { glyph, dialog, outside, closes, close } = await openPopover(t);
	fire(dialog.querySelector('.wqs-group'), 'pointerdown');
	fire(glyph, 'pointerdown');
	assert.equal(closes.length, 0, 'a press inside the dialog or on the glyph closed it');
	fire(outside, 'pointerdown');
	assert.equal(closes.length, 1, 'a press outside left the dialog open');
	assert.ok(dialog.contains(closes[0]), 'focus had already left the dialog before it closed');
	close();
	assert.ok(document.activeElement === glyph, 'closing with focus inside did not return focus to the glyph');
});

test('WAVEQS-8348d every dialog control carries a hover title', async (t) => {
	// [if] a dialog control has no hover title of its own or from its group [then] the hover-title rule is broken, [else stop].
	const { dialog } = await openPopover(t);
	for (const el of dialog.querySelectorAll('button, input, select')) {
		const titled = el.closest('[title]');
		const ok = titled !== null && dialog.contains(titled) && titled.getAttribute('title').trim() !== '';
		assert.ok(ok, `untitled control ${el.id || el.className}`);
	}
});

test('WAVEQS-8348d the glyph imports the popover only on its first click', () => {
	// [if] the compiled glyph imports the popover statically [then] first paint carries its weight, [else stop].
	const path = 'src/lib/components/rb/WaveQuickSettingsButton.svelte';
	const { js } = compile(readFileSync(path, 'utf8'), { filename: path, generate: 'client' });
	assert.match(js.code, /import\(['"]\.\/WaveQuickSettingsPopover\.svelte['"]\)/);
	assert.doesNotMatch(js.code, /^import [^;]*from ['"]\.\/WaveQuickSettingsPopover\.svelte['"]/m);
});
