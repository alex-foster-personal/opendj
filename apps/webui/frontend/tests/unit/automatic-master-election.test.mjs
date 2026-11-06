import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	installAutomaticMasterElectionRunner,
	runAutomaticMasterElection
} from '../../src/lib/rb/automatic-handoff-runners.ts';
import { ScopedCommandScheduler } from '../../src/lib/rb/performance-command-scheduler.ts';

// Exercise the actual runner and scheduler, without replacing an engine or API.
test('automatic master election claims every deck and sync behind prior work', async () => {
	const scheduler = new ScopedCommandScheduler();
	const restore = installAutomaticMasterElectionRunner((work) =>
		scheduler.run([1, 2, 3, 4, 'sync'], work)
	);
	const events = [];
	let release;
	// Hold scope 2 as well as 1 so the [2] follow-up cannot start until `prior`
	// finishes; otherwise it shares no scope with `prior` and races ahead of rejoin.
	const prior = scheduler.run([1, 2], async () => {
		events.push('prior');
		await new Promise((resolve) => {
			release = resolve;
		});
	});
	try {
		const rejoin = runAutomaticMasterElection(async () => {
			events.push('rejoin');
		});
		const later = scheduler.run([2], async () => {
			events.push('later');
		});
		await Promise.resolve();
		await Promise.resolve();
		assert.deepEqual(events, ['prior']);
		release();
		await Promise.all([prior, rejoin, later]);
		assert.deepEqual(events, ['prior', 'rejoin', 'later']);
	} finally {
		restore();
	}
});

test('automatic master election preserves failures and releases the scheduler claim', async () => {
	const scheduler = new ScopedCommandScheduler();
	const restore = installAutomaticMasterElectionRunner((work) =>
		scheduler.run([1, 2, 3, 4, 'sync'], work)
	);
	try {
		const failure = new Error('operation rejected');
		await assert.rejects(
			runAutomaticMasterElection(async () => {
				throw failure;
			}),
			(error) => error === failure
		);
		assert.equal(await scheduler.run(['sync'], async () => 42), 42);
	} finally {
		restore();
	}
});
