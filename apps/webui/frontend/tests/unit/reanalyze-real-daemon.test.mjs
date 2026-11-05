/**
 * NATIVE-5054: Re-analyze from the track menu runs its batch, end to end.
 *
 * The production path with nothing replaced: `runReanalyze` with its DEFAULT
 * dependencies (the generated API client, the real toast store, the real batch
 * watcher), against the real daemon app (fixtures/real_daemon_server.py), whose
 * enqueue route starts the real backfill drain child. Replaces a test that
 * handed the menu a recorder for `enqueueBackfill` and so could only prove the
 * recorder saw `drain: true` (PR #5054 review).
 *
 * - if Re-analyze only plans its batch, it sits "queued, 0 done" forever in a
 *   packaged app and no terminal toast is ever pushed -- broken.
 *
 * The track is real media: the daemon fixture imports a digest-checked copy of
 * the tracked 16 s audio fixture through the production folder import. The
 * item must end `done`, a real own-beatgrid analysis, wherever the host has
 * the producer's two capabilities (the verified Beat This! checkpoint and the
 * canonical decoder), measured by the producer's own checks. Where it lacks
 * one, the only acceptable end is `failed` naming that missing capability,
 * reported as UNAVAILABLE; any other failure fails the test.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { startRealDaemon } from './fixtures/real-daemon.mjs';

/** Covers the child's interpreter start, its pool spawn and one track. */
const SETTLE_TIMEOUT_MS = 100_000;

let daemon;
let mod;

before(async () => {
	daemon = await startRealDaemon();
	mod = await loadTypeScriptModule('tests/unit/fixtures/reanalyze-real-daemon-entry.ts', {
		viteApiBase: daemon.apiBase
	});
});

after(async () => {
	if (daemon) await daemon.stop();
});

async function waitForToast(predicate) {
	const deadline = Date.now() + SETTLE_TIMEOUT_MS;
	while (Date.now() < deadline) {
		const found = mod.toasts.find(predicate);
		if (found) return found;
		await new Promise((resolve) => setTimeout(resolve, 250));
	}
	throw new Error(
		`no matching toast within ${SETTLE_TIMEOUT_MS} ms; toasts: ${JSON.stringify(mod.toasts.map((t) => t.message))}`
	);
}

test('Re-analyze runs the batch it enqueues, through the daemon, to a terminal toast', async (t) => {
	const sid = daemon.reanalyzeSid;
	await mod.runReanalyze([sid]);

	const queued = await waitForToast((t) => /\(batch qb_[0-9a-f]+\)/.test(t.message));
	const batchId = /\(batch (qb_[0-9a-f]+)\)/.exec(queued.message)[1];
	assert.doesNotMatch(
		queued.message,
		/nothing queued/i,
		'positive control: the daemon admitted the track'
	);

	const terminal = await waitForToast(
		(t) => t.message.includes(': complete ') && t.message.includes(batchId)
	);
	const counts = /done (\d+), failed (\d+), skipped (\d+)/.exec(terminal.message);
	assert.ok(counts, `the terminal toast names its counts: ${terminal.message}`);
	const [done, failed, skipped] = counts.slice(1).map(Number);
	assert.equal(done + failed + skipped, 1, 'the one admitted item was settled by the runner');
	assert.equal(done, daemon.beatgrid.available ? 1 : 0, terminal.message);

	// The daemon's own record of the batch, read back over its real endpoint.
	const res = await fetch(
		`${daemon.apiBase}/api/v1/analysis/backfill/progress?batch_id=${batchId}`
	);
	assert.equal(res.status, 200);
	const progress = await res.json();
	assert.equal(progress.state, 'done');
	assert.equal(progress.counts.pending + progress.counts.running, 0);
	const [item] = progress.items;
	assert.equal(item.stable_id, sid);
	if (daemon.beatgrid.available) {
		assert.deepEqual([item.state, item.reason], ['done', null], 'the real track was analyzed');
		return;
	}
	t.diagnostic(`UNAVAILABLE: own beatgrid cannot run on this host (${daemon.beatgrid.detail})`);
	assert.equal(item.state, 'failed', JSON.stringify(item));
	assert.match(
		item.reason,
		/is not available on this host/,
		'only a missing capability may fail it'
	);
	const missing = daemon.beatgrid.detail.split(': ').slice(1).join(': ').slice(0, 60);
	assert.ok(
		item.reason.includes(missing),
		`the reason names the capability the host lacks: ${item.reason}`
	);
});
