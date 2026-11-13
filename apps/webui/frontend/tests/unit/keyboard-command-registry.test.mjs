/**
 * Selector registry acceptance (#12252). Geometry below is synthesized for this test.
 * Regression lines:
 * - if a selector command does not return the live tree's selected id then broken
 * - if leaf drill-in or root drill-out changes selection then broken
 * - if agent discovery omits a selector command, its W A S D / E / Q key, or includes callbacks then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
const selectorKeys = {
	'selector.move.up': 'W',
	'selector.move.down': 'S',
	'selector.move.left': 'A',
	'selector.move.right': 'D',
	'selector.drill-in': 'E',
	'selector.drill-out': 'Q'
};
const selectorIds = Object.keys(selectorKeys);

function tree(initialId = 'root') {
	const node = (id, parentId, x, y, kind = 'leaf') => ({
		id,
		parentId,
		kind,
		rect: { x, y, width: 100, height: 100 }
	});
	return createSelectorTree(
		[
			node('root', null, 0, 0, 'section'),
			node('other', null, 300, 0, 'section'),
			node('D', 'root', 120, 120),
			node('C', 'root', 0, 120),
			node('B', 'root', 120, 0),
			node('A', 'root', 0, 0)
		],
		initialId
	);
}

function run(selector, id, expected) {
	assert.equal(registry.runCommand(id, { timeStamp: 0, selector }), expected);
	assert.equal(selector.selected(), expected);
}

test('all four registered moves return and publish the selected sibling id', () => {
	const selector = tree('A');
	for (const [direction, expected] of [
		['right', 'B'],
		['down', 'D'],
		['left', 'C'],
		['up', 'A']
	])
		run(selector, `selector.move.${direction}`, expected);
	run(selector, 'selector.move.left', 'A');
	run(selector, 'selector.move.up', 'A');
	const roots = tree();
	run(roots, 'selector.move.right', 'other');
});

test('drill commands select the top-left child, remember it, and preserve leaf/root no-ops', () => {
	const selector = tree();
	run(selector, 'selector.drill-out', 'root');
	run(selector, 'selector.drill-in', 'A');
	run(selector, 'selector.drill-in', 'A');
	run(selector, 'selector.move.down', 'C');
	run(selector, 'selector.drill-out', 'root');
	run(selector, 'selector.drill-in', 'C');
});

test('agent discovery is serializable and lists every selector command with its key without mutating', () => {
	const selector = tree();
	const metadata = registry.listPerformanceCommandMetadata();
	assert.deepEqual(JSON.parse(JSON.stringify(metadata)), metadata);
	for (const id of selectorIds) {
		const def = registry.PERFORMANCE_COMMAND_REGISTRY.get(id);
		assert.ok(def, id);
		assert.ok(registry.listPerformanceCommands().some((command) => command.id === id));
		assert.deepEqual(
			metadata.find((command) => command.id === id),
			{
				id,
				label: def.label,
				scopes: ['global'],
				scope: 'global',
				defaultKeys: [selectorKeys[id]]
			}
		);
		assert.ok(def.label.trim().length > 0);
	}
	assert.equal(selector.selected(), 'root');
	for (const command of metadata) assert.equal('run' in command, false);
});

test('selector commands fail explicitly without a host; unknown ids and invalid definitions fail', () => {
	for (const id of selectorIds) {
		assert.throws(() => registry.runCommand(id, { timeStamp: 0 }), /no selector host/);
	}
	assert.throws(() => registry.runCommand('selector.unknown', { timeStamp: 0 }), /unknown command/);
	const def = registry.PERFORMANCE_COMMAND_REGISTRY.get('preset.run.bass-swap-8');
	assert.throws(() => registry.buildPerformanceCommandRegistry([def, def]), /duplicate command id/);
	assert.throws(
		() => registry.buildPerformanceCommandRegistry([{ ...def, label: ' ' }]),
		/empty label/
	);
	assert.throws(
		() => registry.runCommand(def.id, { timeStamp: 0, selector: tree() }),
		/no mixer host/
	);
});
