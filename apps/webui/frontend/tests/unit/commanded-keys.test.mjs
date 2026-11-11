/**
 * Digit position keys and Cmd/Ctrl+digit jumps on selected dials/faders (#9453).
 *
 * Regression lines:
 * - if channel 4 LOW is selected and 5 is pressed then no 4-beat glide to 0.5 is planned
 * - if 1/9/3/7 are pressed then targets are not 0 / 1 / 0.25 / 0.75
 * - if Cmd+2 is pressed then beats are not 0 and value is not 0.125 immediately
 * - if nothing is selected and 2 is pressed then deck layout Cmd+2 must still resolve
 * - if two knobs are shift-selected and 5 is pressed then both must move to 0.5
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const COMMANDED_SRC = fileURLToPath(new URL('../../src/lib/rb/commanded-keys.ts', import.meta.url));

let ck;
let expr;

before(async () => {
	ck = await loadTypeScriptModule('src/lib/rb/commanded-keys.ts');
	expr = await loadTypeScriptModule('src/lib/rb/animate-expr.ts');
});

function makeDeps({
	selectedIds = [],
	values = new Map(),
	isTyping = () => false,
	settingsOpen = () => false
} = {}) {
	const calls = { setAbsolute: [], startGlides: [], prevented: 0, stopped: 0 };
	const deps = {
		selectedIds: () => selectedIds,
		readValue: (id) => (values.has(id) ? values.get(id) : null),
		setAbsolute: (id, v) => {
			calls.setAbsolute.push([id, v]);
			values.set(id, v);
		},
		startGlides: (moves) => calls.startGlides.push([...moves]),
		isTyping,
		settingsOpen
	};
	const run = (partial) => {
		let prevented = false;
		let stopped = false;
		const event = {
			preventDefault: () => {
				prevented = true;
				calls.prevented += 1;
			},
			stopPropagation: () => {
				stopped = true;
				calls.stopped += 1;
			},
			...partial
		};
		const result = ck.handleCommandedKeydown(event, deps);
		return { result, calls, prevented, stopped, values };
	};
	return { run, deps, calls, values };
}

test('COMMANDED_POSITIONS matches the 1-9 table', () => {
	const expected = [0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1];
	for (let digit = 1; digit <= 9; digit += 1) {
		assert.equal(ck.COMMANDED_POSITIONS[digit], expected[digit - 1]);
	}
});

test('bare digits 1, 3, 7, 9 set the expected targets on glide', () => {
	const cases = [
		['1', 0],
		['3', 0.25],
		['7', 0.75],
		['9', 1]
	];
	for (const [key, to] of cases) {
		const { calls } = makeDeps({
			selectedIds: ['4:low'],
			values: new Map([['4:low', 0.5]])
		}).run({ key, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false });
		assert.equal(calls.startGlides.length, 1);
		assert.equal(calls.startGlides[0][0].to, to);
	}
});

test('if 4:low is selected and 5 is pressed then a 4-beat glide to 0.5 is planned', () => {
	const { result, calls, prevented, stopped } = makeDeps({
		selectedIds: ['4:low'],
		values: new Map([['4:low', 0.2]])
	}).run({ key: '5', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false });
	assert.ok(result);
	assert.deepEqual(calls.startGlides, [[{ controlId: '4:low', from: 0.2, to: 0.5, beats: 4 }]]);
	assert.equal(calls.setAbsolute.length, 0);
	assert.ok(prevented && stopped);
});

test('Cmd+2 or Ctrl+2 jumps to 0.125 with beats 0', () => {
	for (const mod of [
		{ metaKey: true, ctrlKey: false },
		{ metaKey: false, ctrlKey: true }
	]) {
		const bag = makeDeps({
			selectedIds: ['4:low'],
			values: new Map([['4:low', 0.9]])
		});
		const { result, calls } = bag.run({
			key: '2',
			...mod,
			altKey: false,
			shiftKey: false
		});
		assert.ok(result);
		assert.equal(result[0].beats, 0);
		assert.deepEqual(calls.setAbsolute, [['4:low', 0.125]]);
		assert.equal(bag.values.get('4:low'), 0.125);
		assert.equal(calls.startGlides.length, 0);
	}
});

test('empty selection does not claim digit or Cmd+2; deck layout Cmd+2 still resolves', async () => {
	const bag = makeDeps({ selectedIds: [] });
	const bare = bag.run({
		key: '2',
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false
	});
	assert.equal(bare.result, null);
	assert.equal(bare.calls.startGlides.length, 0);
	assert.equal(bare.prevented, false);
	const cmd = bag.run({ key: '2', metaKey: true, ctrlKey: false, altKey: false, shiftKey: false });
	assert.equal(cmd.result, null);
	assert.equal(cmd.stopped, false);
	const layout = await loadTypeScriptModule('src/lib/rb/deck-layout-hotkeys.ts');
	assert.equal(
		layout.resolveDeckLayoutHotkeyMode(
			{ key: '2', metaKey: true, ctrlKey: false, altKey: false, shiftKey: false, target: null },
			{ settingsOpen: false }
		),
		'less'
	);
});

test('two selected knobs each get a glide to 0.5', () => {
	const { calls } = makeDeps({
		selectedIds: ['1:low', '2:low'],
		values: new Map([
			['1:low', 0.1],
			['2:low', 0.9]
		])
	}).run({ key: '5', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false });
	assert.equal(calls.startGlides.length, 1);
	assert.deepEqual(calls.startGlides[0], [
		{ controlId: '1:low', from: 0.1, to: 0.5, beats: 4 },
		{ controlId: '2:low', from: 0.9, to: 0.5, beats: 4 }
	]);
});

test('unreadable selected id is skipped; all unreadable yields null', () => {
	const partial = makeDeps({
		selectedIds: ['1:low', 'ghost:low'],
		values: new Map([['1:low', 0.4]])
	}).run({ key: '5', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false });
	assert.equal(partial.calls.startGlides[0].length, 1);
	const none = makeDeps({
		selectedIds: ['ghost:low'],
		values: new Map()
	}).run({ key: '5', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false });
	assert.equal(none.result, null);
	assert.equal(none.prevented, false);
});

test('typing, settings, Alt, Shift and 0 do not claim', () => {
	const blocked = [
		{ isTyping: () => true },
		{ settingsOpen: () => true },
		{ partial: { altKey: true } },
		{ partial: { shiftKey: true } },
		{ partial: { key: '0' } }
	];
	for (const row of blocked) {
		const bag = makeDeps({
			selectedIds: ['1:low'],
			values: new Map([['1:low', 0.5]]),
			isTyping: row.isTyping,
			settingsOpen: row.settingsOpen
		});
		const partial = {
			key: '5',
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			...(row.partial ?? {})
		};
		const { result, prevented } = bag.run(partial);
		assert.equal(result, null);
		assert.equal(prevented, false);
	}
});

test('repeat claims but does not restart glides', () => {
	const { calls, prevented } = makeDeps({
		selectedIds: ['4:low'],
		values: new Map([['4:low', 0.2]])
	}).run({
		key: '5',
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: true
	});
	assert.ok(prevented);
	assert.equal(calls.startGlides.length, 0);
});

test('commandedScopeOf classifies fader vs dial', () => {
	assert.equal(ck.commandedScopeOf([]), null);
	assert.equal(ck.commandedScopeOf(['1:fader']), 'fader');
	assert.equal(ck.commandedScopeOf(['crossfader']), 'fader');
	assert.equal(ck.commandedScopeOf(['1:fader', '1:low']), 'dial');
});

test('COMMANDED_KEY_COMMANDS has 18 unique registry-shaped entries', () => {
	assert.equal(ck.COMMANDED_KEY_COMMANDS.length, 18);
	const ids = new Set();
	const keys = new Set();
	for (const row of ck.COMMANDED_KEY_COMMANDS) {
		assert.ok(row.id.startsWith('dial.'));
		assert.deepEqual(row.scopes, ['dial', 'fader']);
		assert.ok(!ids.has(row.id));
		ids.add(row.id);
		for (const k of row.defaultKeys) {
			assert.ok(!keys.has(k), `duplicate defaultKeys ${k}`);
			keys.add(k);
		}
	}
});

test('commandedRampExpr parses and rejects beats 0', () => {
	const move = { controlId: '1:low', from: 0, to: 0.125, beats: 4 };
	const ramp = ck.commandedRampExpr(move);
	const parsed = expr.parseAnimateExpr(ramp);
	assert.equal(parsed.ok, true);
	assert.equal(parsed.to, 0.125);
	assert.equal(parsed.over.n, 4);
	assert.throws(() => ck.commandedRampExpr({ ...move, beats: 0 }), RangeError);
});

test('commanded-keys.ts has no runtime imports', () => {
	const source = readFileSync(COMMANDED_SRC, 'utf8');
	assert.doesNotMatch(source, /^import (?!type )/m);
});

test('if the command registry lacks any of the 18 dial ids then broken', async () => {
	const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
	const listed = registry.listPerformanceCommands();
	for (const digit of [1, 2, 3, 4, 5, 6, 7, 8, 9]) {
		for (const id of [`dial.position.${digit}`, `dial.jump.${digit}`]) {
			const def = listed.find((d) => d.id === id);
			assert.ok(def, `${id} not registered`);
			assert.ok(def.scopes.includes('dial') && def.scopes.includes('fader'), `${id} scopes`);
		}
	}
	const set = [];
	const glides = [];
	const deps = {
		selectedIds: () => ['ch4.low', 'ch4.mid'],
		readValue: () => 0.9,
		setAbsolute: (id, v) => set.push([id, v]),
		startGlides: (moves) => glides.push(...moves),
		isTyping: () => false,
		settingsOpen: () => false
	};
	registry.runCommand('dial.jump.2', { timeStamp: 0, commanded: deps });
	assert.deepEqual(set, [
		['ch4.low', 0.125],
		['ch4.mid', 0.125]
	]);
	registry.runCommand('dial.position.5', { timeStamp: 0, commanded: deps });
	assert.deepEqual(
		glides.map((m) => [m.to, m.beats]),
		[
			[0.5, 4],
			[0.5, 4]
		]
	);
	assert.throws(
		() => registry.runCommand('dial.jump.1', { timeStamp: 0 }),
		/no dial or fader host/
	);
});

test('performanceCommandsInScope lists dial commands in both dial and fader scope', async () => {
	const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
	const dial = registry.performanceCommandsInScope('dial');
	const fader = registry.performanceCommandsInScope('fader');
	const global = registry.performanceCommandsInScope('global');
	assert.equal(dial.length, 18);
	assert.equal(fader.length, 18);
	for (const def of dial) {
		assert.ok(def.scopes.includes('dial') && def.scopes.includes('fader'));
	}
	assert.ok(global.length > 0);
	assert.ok(global.every((def) => def.id.startsWith('preset.run.')));
});

test('registryCommandCandidates builds Cmd-K performance rows', async () => {
	const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
	const cmdk = await loadTypeScriptModule('src/lib/rb/cmdk-actions.ts');
	const rows = cmdk.registryCommandCandidates(registry.performanceCommandsInScope('dial'));
	assert.equal(rows.length, 18);
	assert.ok(rows.every((row) => row.command.type === 'performance'));
	const jump2 = rows.find((row) => row.id === 'dial.jump.2');
	const pos5 = rows.find((row) => row.id === 'dial.position.5');
	assert.equal(jump2.hint, 'Cmd 2');
	assert.equal(pos5.hint, '5');
	const filtered = cmdk.actionRows('jump 2', rows, []);
	assert.ok(filtered.some((row) => row.id === 'dial.jump.2'));
});

test('handleCommandedKeydown with run delegates without applying moves', () => {
	const bag = makeDeps({
		selectedIds: ['1:low'],
		values: new Map([['1:low', 0.2]])
	});
	let runCalls = 0;
	const result = ck.handleCommandedKeydown(
		{
			key: '5',
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			preventDefault: () => {},
			stopPropagation: () => {}
		},
		bag.deps,
		() => {
			runCalls += 1;
		}
	);
	assert.ok(result);
	assert.equal(runCalls, 1);
	assert.equal(bag.calls.startGlides.length, 0);
	assert.equal(bag.calls.setAbsolute.length, 0);
});

test('handleCommandedKeydown with run does not invoke run on repeat', () => {
	const bag = makeDeps({
		selectedIds: ['1:low'],
		values: new Map([['1:low', 0.2]])
	});
	let runCalls = 0;
	ck.handleCommandedKeydown(
		{
			key: '5',
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			repeat: true,
			preventDefault: () => {},
			stopPropagation: () => {}
		},
		bag.deps,
		() => {
			runCalls += 1;
		}
	);
	assert.equal(runCalls, 0);
});

test('PRRT_kwDOSEvNd861F5GB: the dial commands are run through the command registry by the key handler', () => {
	const knobs = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/knob-control.svelte.ts', import.meta.url)),
		'utf8'
	);
	assert.match(knobs, /runPerformanceCommand\(/);
});
