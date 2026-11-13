// requirement: EDGESAMP-13957
// Edge sampler key policy (#13720): existing /performance bindings win over the sampler.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let keys;
let routing;
let registry;
before(async () => {
	[keys, routing, registry] = await Promise.all([
		loadTypeScriptModule('src/lib/player/sampler/sampler-keys.ts'),
		loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts'),
		loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts')
	]);
});

const press = (key, over = {}) => ({
	key,
	code: /^[0-9]$/.test(key) ? `Digit${key}` : '',
	target: null,
	metaKey: false,
	ctrlKey: false,
	altKey: false,
	shiftKey: false,
	defaultPrevented: false,
	...over
});
const ASLEEP = { inTextField: false, awakeEdge: null };
const awake = (edge) => ({ inTextField: false, awakeEdge: edge });

test('digits 1 to 8 go to hot cues while no edge is awake', () => {
	/** [if] the sampler is enabled and no edge is awake [then] digits 1 to 8 reach the hot cue handler, [else stop]. */
	for (const [i, slot] of [...'ABCDEFGH'].entries()) {
		const key = String(i + 1);
		assert.equal(keys.samplerKeyAction(press(key), ASLEEP), null, `${key} taken by the sampler`);
		assert.deepEqual(routing.resolvePerformanceShortcutAction(press(key)), {
			kind: 'hot-cue-trigger',
			slot,
			repeat: false
		});
	}
});

test('[ and ] stay the waveform zoom with the sampler on, awake or not', () => {
	/** [if] the sampler is enabled [then] [ and ] still zoom the waveform, asleep or awake, [else stop]. */
	for (const ctx of [ASLEEP, awake('L'), awake('R')]) {
		assert.equal(keys.samplerKeyAction(press('['), ctx), null);
		assert.equal(keys.samplerKeyAction(press(']'), ctx), null);
	}
	assert.ok(!keys.SAMPLER_KEYS.includes('[') && !keys.SAMPLER_KEYS.includes(']'));
	assert.deepEqual(routing.resolvePerformanceShortcutAction(press('[')), {
		kind: 'wave-zoom',
		direction: 'out'
	});
	assert.deepEqual(routing.resolvePerformanceShortcutAction(press(']')), {
		kind: 'wave-zoom',
		direction: 'in'
	});
	const zoomRows = registry.HOTKEY_REGISTRY.filter((e) => e.chord === '[' || e.chord === ']');
	for (const row of zoomRows) assert.doesNotMatch(row.description, /sampler/i, row.id);
});

test('a digit fires the awake edge tab and is claimed before hot cues', () => {
	/** [if] an edge is awake [then] a digit fires that edge's tab (1 to 0 = tabs 1 to 10), not hot cues, [else stop]. */
	assert.deepEqual(keys.samplerKeyAction(press('1'), awake('L')), {
		kind: 'fire',
		edge: 'L',
		index: 0
	});
	assert.deepEqual(keys.samplerKeyAction(press('3'), awake('R')), {
		kind: 'fire',
		edge: 'R',
		index: 2
	});
	assert.deepEqual(keys.samplerKeyAction(press('0'), awake('R')), {
		kind: 'fire',
		edge: 'R',
		index: 9
	});
	assert.equal(keys.awakeSamplerEdge({ L: true, R: false }, 'R'), 'L');
	assert.equal(keys.awakeSamplerEdge({ L: true, R: true }, 'R'), 'R');
	assert.equal(keys.awakeSamplerEdge({ L: false, R: false }, 'L'), null);
});

test('PageUp and PageDown page the awake edge set and clash with no page binding', () => {
	/** [if] PageUp / PageDown is pressed with an edge awake [then] that edge's set pages back / forward, [else stop]. */
	assert.deepEqual(keys.samplerKeyAction(press('PageDown'), awake('L')), {
		kind: 'flip',
		edge: 'L',
		delta: 1
	});
	assert.deepEqual(keys.samplerKeyAction(press('PageUp'), awake('R')), {
		kind: 'flip',
		edge: 'R',
		delta: -1
	});
	assert.equal(keys.samplerKeyAction(press('PageDown'), ASLEEP), null);
	for (const key of ['PageUp', 'PageDown'])
		assert.equal(routing.resolvePerformanceShortcutAction(press(key), { gridJump: true }), null);
	const page = registry.HOTKEY_REGISTRY.find((e) => e.id === 'edge-sampler-page');
	assert.equal(page?.chord, 'PageUp / PageDown');
});

test('a claimed key, a modifier chord or a text field keeps the sampler out', () => {
	/** [if] the key was claimed earlier, has Cmd/Ctrl/Alt, or a text field has focus [then] no sampler action, [else stop]. */
	assert.equal(
		keys.samplerKeyAction(press('PageUp', { defaultPrevented: true }), awake('R')),
		null
	);
	for (const mod of ['metaKey', 'ctrlKey', 'altKey'])
		assert.equal(keys.samplerKeyAction(press('3', { [mod]: true }), awake('R')), null, mod);
	assert.equal(keys.samplerKeyAction(press('3'), { inTextField: true, awakeEdge: 'R' }), null);
});
