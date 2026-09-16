/**
 * UI-mirror control addressability (#3196).
 *
 * Regression lines:
 * - if two controls share a preferred name then both survive in the map with distinct keys
 * - if a generated suffix is already taken then the next free suffix is used
 * - if N controls are scanned then the map holds N entries
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let controls;

before(async () => {
	controls = await loadTypeScriptModule('src/lib/rb/ui-mirror-controls.ts');
});

function _element(attrs = {}, classList = []) {
	return {
		getAttribute(name) {
			return attrs[name] ?? null;
		},
		textContent: attrs.textContent ?? '',
		classList: {
			contains(cls) {
				return classList.includes(cls);
			}
		}
	};
}

test('buildControlsMap keeps one entry per element', () => {
	const elements = [
		_element({ 'data-testid': 'play-deck-1' }),
		_element({ 'data-testid': 'play-deck-2' }),
		_element({ 'aria-label': 'master mute' })
	];
	const map = controls.buildControlsMap(elements);
	assert.equal(Object.keys(map).length, 3);
});

test('duplicate preferred names receive deterministic suffixes and keep both statuses', () => {
	const elements = [
		_element({ 'aria-label': 'restart loop' }, ['rb-inert']),
		_element({ 'aria-label': 'restart loop' })
	];
	const map = controls.buildControlsMap(elements);
	assert.deepEqual(map, {
		'restart loop': 'inert',
		'restart loop#2': 'available'
	});
});

test('uniqueControlKey skips a suffix already claimed by another control', () => {
	const used = new Set(['restart loop', 'restart loop#2']);
	assert.equal(controls.uniqueControlKey('restart loop', used), 'restart loop#3');
	assert.equal(controls.uniqueControlKey('restart loop', used), 'restart loop#4');
});

test('controlPreferredName falls back to trimmed text then index', () => {
	assert.equal(controls.controlPreferredName(_element({ textContent: '  STEM  ' }), 0), 'STEM');
	assert.equal(controls.controlPreferredName(_element({ textContent: '   ' }), 4), 'control-5');
});
