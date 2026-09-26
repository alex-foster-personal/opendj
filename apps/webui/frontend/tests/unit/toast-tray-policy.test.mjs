/**
 * @pytest.mark.requirement UX-TOAST-03
 * [if] four toasts pushed [then] only three are visible candidates [else stop].
 * [if] oldest evicted [then] it is marked exiting before removal [else stop].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let policy;

before(async () => {
	policy = await loadTypeScriptModule('src/lib/toast-tray-policy.ts');
});

test('selectVisibleToasts keeps at most three non-exiting toasts', () => {
	const all = [
		{ id: 1, exiting: false },
		{ id: 2, exiting: false },
		{ id: 3, exiting: false },
		{ id: 4, exiting: false }
	];
	const visible = policy.selectVisibleToasts(all);
	const nonExitingVisible = visible.filter((t) => t.exiting !== true);
	assert.equal(nonExitingVisible.length, 3);
	assert.deepEqual(
		nonExitingVisible.map((t) => t.id),
		[2, 3, 4]
	);
});

test('selectVisibleToasts still shows exiting toasts for animation', () => {
	const all = [
		{ id: 1, exiting: true },
		{ id: 2, exiting: false },
		{ id: 3, exiting: false },
		{ id: 4, exiting: false }
	];
	const visible = policy.selectVisibleToasts(all);
	assert.ok(visible.some((t) => t.id === 1 && t.exiting === true));
});

test('oldestNonExitingIndex finds the first active toast', () => {
	const all = [{ exiting: true }, { exiting: false }, { exiting: false }];
	assert.equal(policy.oldestNonExitingIndex(all), 1);
});
