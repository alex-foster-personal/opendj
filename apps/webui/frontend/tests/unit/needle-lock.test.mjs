// requirement: DECKCTL-10421
// [if] Needle Lock is missing from the settings catalogue or defaults on [then] broken
// [if] with Needle Lock on a click on a playing deck's strip still seeks, or a paused deck stops seeking [then] broken
// [if] Needle Lock gates the main scrolling waveform or keyboard jumps [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const read = (rel) => readFileSync(`${SRC}/${rel}`, 'utf8');

let lock;
let defaults;
let catalog;

before(async () => {
	lock = await loadTypeScriptModule('src/lib/rb/needle-lock.ts');
	defaults = await loadTypeScriptModule('src/lib/rb/deck-default-prefs.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
});

test('the setting exists in the catalogue as an implemented boolean, default off', () => {
	const entry = catalog.SETTINGS_CATALOG.find((setting) => setting.id === 'deck_needle_lock');
	assert.ok(entry, 'deck_needle_lock is in the settings catalogue');
	assert.equal(entry.label, 'Needle Lock');
	assert.equal(entry.implemented, true);
	assert.deepEqual(entry.control, { kind: 'boolean' });
	assert.equal(defaults.DECK_DEFAULT_PREF_DEFAULTS.deck_needle_lock, false);
	assert.deepEqual(defaults.mergeDeckDefaultPrefsFromParsed({}, 'k'), {
		deck_default_quantize: true,
		deck_needle_lock: false
	});
	assert.deepEqual(
		defaults.mergeDeckDefaultPrefsFromParsed({ deck_needle_lock: true }, 'k').deck_needle_lock,
		true
	);
	assert.throws(
		() => defaults.mergeDeckDefaultPrefsFromParsed({ deck_needle_lock: 'on' }, 'k'),
		/deck_needle_lock is not a boolean/
	);
});

test('the setter persists, syncs to disk and announces', () => {
	const state = { ...defaults.DECK_DEFAULT_PREF_DEFAULTS };
	const synced = [];
	let persisted = 0;
	const seen = [];
	const off = defaults.onDeckDefaultsChange((prefs) => seen.push(prefs.deck_needle_lock));
	const { setDeckNeedleLock } = defaults.makeDeckDefaultPrefSetters(
		state,
		() => (persisted += 1),
		(patch) => synced.push(patch)
	);
	setDeckNeedleLock(true);
	off();
	assert.equal(state.deck_needle_lock, true);
	assert.equal(persisted, 1);
	assert.deepEqual(synced, [{ deck_needle_lock: true }]);
	assert.deepEqual(seen, [true]);
	assert.equal(defaults.currentDeckDefaults().deck_needle_lock, true);
	assert.throws(() => setDeckNeedleLock('yes'), /deck_needle_lock must be a boolean/);
});

test('only a playing deck with Needle Lock on refuses the strip seek, with a hint', () => {
	assert.equal(lock.needleLockRefusal(true, true), lock.NEEDLE_LOCK_HINT);
	assert.equal(lock.needleLockRefusal(true, false), null, 'a paused deck still seeks');
	assert.equal(lock.needleLockRefusal(false, true), null, "off: today's behaviour");
	assert.equal(lock.needleLockRefusal(false, false), null);
});

test('the performance hotkeys never consult Needle Lock: only the strip click does', () => {
	const text = read('lib/rb/performance-hotkeys.ts');
	assert.doesNotMatch(
		text,
		/deck_needle_lock|needleLockRefusal/,
		'performance-hotkeys.ts must not read Needle Lock'
	);
});
