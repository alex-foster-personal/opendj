/** INSTALL-32: the right-click stems menu hides tiers the engine cannot spawn. */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { menuStemTiers } = await loadTypeScriptModule('src/lib/rb/stem-tier-menu.ts');

function tier(key, where, runnable, availability = 'AVAILABLE') {
	return {
		key,
		name: key,
		where,
		availability,
		unavailable_because: availability === 'AVAILABLE' ? '' : 'measured, not offered',
		runnable_here: runnable,
		not_runnable_because: runnable ? null : 'needs a development checkout with uv and modal'
	};
}

test('installed app: Modal tiers are hidden, LOCAL stays', () => {
	const shown = menuStemTiers([
		tier('LOCAL', 'local', true),
		tier('S', 'modal', false),
		tier('M', 'modal', false),
		tier('L', 'modal', false)
	]);
	assert.deepEqual(
		shown.map((t) => t.key),
		['LOCAL']
	);
});

test('checkout: every runnable tier stays, in ladder order', () => {
	const shown = menuStemTiers([
		tier('LOCAL', 'local', true),
		tier('S', 'modal', true),
		tier('M', 'modal', true),
		tier('L', 'modal', true)
	]);
	assert.deepEqual(
		shown.map((t) => t.key),
		['LOCAL', 'S', 'M', 'L']
	);
});

test('a NOT_APPLICABLE rung the engine could run is not hidden by this rule', () => {
	// It keeps its inert "(n/a)" item; hiding is only for runnable_here=false.
	const shown = menuStemTiers([tier('S', 'modal', true, 'NOT_APPLICABLE')]);
	assert.deepEqual(
		shown.map((t) => t.key),
		['S']
	);
});

test('the menu module is wired to the filter', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(
		new URL('../../src/lib/components/rb/QuickDrawMenu.svelte', import.meta.url),
		'utf8'
	);
	assert.match(source, /menuStemTiers\(stemTiers\)\.map\(/);
});
