// requirement: METERCAL-5586
/**
 * #5586: the mixer's R|M level calibration is a typed performance command,
 * so an agent (or a LESS user, whose view hides the buttons) arms and
 * disarms it exactly as a MORE click does.
 *
 * Regression lines:
 * - if an agent sends level_calibration enabled: true for a playing deck then
 *   that kind captures the deck's level and its *_enabled pref (the R/M
 *   aria-pressed source) becomes true
 * - if the deck is stopped then arming is refused with the button's reason
 *   and the pref stays unarmed
 * - if the command sends enabled: false then that kind disarms even while the
 *   deck is stopped, keeping the captured number
 * - if the R/M click stops going through the shared apply path then the
 *   command and the click can drift apart
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let entry;
let uninstall;
let restoreStorage;

/** Give prefs a throwaway localStorage so persistence writes are inert. */
function installPrefsStorage() {
	const store = new Map();
	const storage = {
		getItem: (key) => (store.has(key) ? store.get(key) : null),
		setItem: (key, value) => store.set(key, value),
		removeItem: (key) => store.delete(key)
	};
	const original = globalThis.localStorage;
	globalThis.localStorage = storage;
	globalThis.window = { localStorage: storage };
	return () => {
		if (original === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = original;
		delete globalThis.window;
	};
}

before(async () => {
	globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
	entry = await loadTypeScriptModule('tests/unit/fixtures/level-calibration-ipc-entry.ts');
	restoreStorage = installPrefsStorage();
	uninstall = entry.installPerformanceBrowserIpc();
});

after(() => {
	uninstall?.();
	restoreStorage?.();
	entry?.setLevelCalibrationSourcesForTest(null);
});

beforeEach(() => {
	Object.assign(entry.uiPrefs.level_calibration, {
		red_dbfs: null,
		red_enabled: false,
		ceiling_dbfs: null,
		ceiling_enabled: false
	});
	entry.setLevelCalibrationSourcesForTest(null);
	entry.deckStates[1].playing = false;
});

test('level_calibration parses; a bad kind or non-boolean enabled is refused', () => {
	assert.deepEqual(
		entry.parsePerformanceCommandForTest({
			type: 'level_calibration',
			deck: 2,
			kind: 'ceiling',
			enabled: false
		}),
		{ type: 'level_calibration', deck: 2, kind: 'ceiling', enabled: false }
	);
	assert.throws(
		() =>
			entry.parsePerformanceCommandForTest({
				type: 'level_calibration',
				deck: 1,
				kind: 'green',
				enabled: true
			}),
		/kind must be red\|ceiling/
	);
	assert.throws(
		() =>
			entry.parsePerformanceCommandForTest({
				type: 'level_calibration',
				deck: 1,
				kind: 'red',
				enabled: 'yes'
			}),
		/enabled/
	);
});

test('an agent arm on a playing deck captures the level and sets the aria-pressed pref', async () => {
	entry.setLevelCalibrationSourcesForTest({
		playingOf: () => true,
		readingOf: () => ({ db: -4.5, heardDb: -6 })
	});
	for (const kind of ['red', 'ceiling']) {
		await window.musicDjToolsPerformance.dispatch({
			type: 'level_calibration',
			deck: 1,
			kind,
			enabled: true
		});
	}
	assert.equal(entry.uiPrefs.level_calibration.red_enabled, true);
	assert.equal(entry.uiPrefs.level_calibration.red_dbfs, -4.5);
	assert.equal(entry.uiPrefs.level_calibration.ceiling_enabled, true);
	assert.equal(entry.uiPrefs.level_calibration.ceiling_dbfs, -4.5);
});

test('arming a stopped deck is refused with the button reason and stays unarmed', async () => {
	assert.equal(entry.deckStates[1].playing, false);
	const result = await entry.runPerformanceCommandFromUi({
		type: 'level_calibration',
		deck: 1,
		kind: 'red',
		enabled: true
	});
	assert.equal(result.ok, false);
	const message = result.error instanceof Error ? result.error.message : String(result.error);
	assert.ok(message.includes(entry.LEVEL_CALIBRATION_REFUSAL), message);
	assert.equal(entry.uiPrefs.level_calibration.red_enabled, false);
	assert.equal(entry.uiPrefs.level_calibration.red_dbfs, null);
});

test('disarm works while the deck is stopped and keeps the captured number', async () => {
	Object.assign(entry.uiPrefs.level_calibration, { ceiling_dbfs: -2, ceiling_enabled: true });
	await window.musicDjToolsPerformance.dispatch({
		type: 'level_calibration',
		deck: 1,
		kind: 'ceiling',
		enabled: false
	});
	assert.equal(entry.uiPrefs.level_calibration.ceiling_enabled, false);
	assert.equal(entry.uiPrefs.level_calibration.ceiling_dbfs, -2);
});

test('the R/M click runs the same apply path', () => {
	// SSR never fires onclick, so the click wiring is pinned in the script text;
	// the aria-pressed side is rendered below.
	const strip = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/rb/mixer/ChannelStrip.svelte', import.meta.url)
		),
		'utf8'
	);
	assert.match(strip, /applyLevelCalibration\(deckId, kind, !enabled\)/);
});

const noop = () => {};

function stripProps(deckId) {
	return {
		deckId,
		less: false,
		trim: 0.5,
		eqHigh: 0.5,
		eqMid: 0.5,
		eqLow: 0.5,
		filter: 0.5,
		fader: 1,
		cueEnabled: false,
		mixerStems: false,
		stemPending: false,
		commandPending: false,
		commandError: null,
		ontrim: noop,
		onAutoGainOffset: async () => ({ ok: true }),
		oneq: noop,
		onfilter: noop,
		oncolorfx: noop,
		onechodivision: noop,
		oncolorfxall: noop,
		onfader: noop,
		oncue: noop,
		onStemEqMode: noop,
		onStemGain: noop,
		onStemMute: async () => {},
		onStemSolo: async () => {}
	};
}

/** aria-pressed of the rendered button whose aria-label starts with `label`. */
function pressedOf(html, label) {
	const at = html.indexOf(`aria-label="${label}`);
	assert.ok(at >= 0, `the strip renders the ${label} button`);
	const open = html.lastIndexOf('<button', at);
	const tag = html.slice(open, html.indexOf('>', at));
	const m = tag.match(/aria-pressed="(true|false)"/);
	assert.ok(m, `${label} carries aria-pressed: ${tag}`);
	return m[1] === 'true';
}

test('the rendered R/M aria-pressed reads the level_calibration prefs', async () => {
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	// The prefs stub window has no event API; the strip's knob module binds one at import.
	window.addEventListener ??= noop;
	window.removeEventListener ??= noop;
	const ssr = await loadSvelteSsrModule(`
		export { render } from 'svelte/server';
		export { default as ChannelStrip } from '$lib/components/rb/mixer/ChannelStrip.svelte';
		export { uiPrefs } from '$lib/rb/prefs.svelte';
	`);
	const render = () => ssr.render(ssr.ChannelStrip, { props: stripProps(1) }).body;
	for (const [red, ceiling] of [
		[true, false],
		[false, true]
	]) {
		Object.assign(ssr.uiPrefs.level_calibration, {
			red_dbfs: -4,
			red_enabled: red,
			ceiling_dbfs: -2,
			ceiling_enabled: ceiling
		});
		const html = render();
		assert.equal(pressedOf(html, 'meter red anchor channel 1'), red);
		assert.equal(pressedOf(html, 'master ceiling channel 1'), ceiling);
	}
});
