import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ticker;

before(async () => {
	ticker = await loadTypeScriptModule('src/lib/rb/mirror-publish-ticker.ts');
});

test('MIRROR_PUBLISH_INTERVAL_MS stays at one second', () => {
	assert.equal(ticker.MIRROR_PUBLISH_INTERVAL_MS, 1000);
});

test('the worker ticker source uses a dedicated Worker interval, not main-thread setInterval', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const source = readFileSync(`${root}/src/lib/rb/mirror-publish-ticker.ts`, 'utf8');
	assert.match(source, /new Worker/);
	assert.match(source, /MIRROR_PUBLISH_INTERVAL_MS/);
	assert.doesNotMatch(source, /window\.setInterval/);
});

test('ui-mirror uses the worker ticker instead of main-thread setInterval', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const source = readFileSync(`${root}/src/lib/rb/ui-mirror.ts`, 'utf8');
	assert.match(source, /startMirrorPublishTicker/);
	assert.doesNotMatch(source, /setInterval\(mirror\.publish/);
});

test('a worker tick calls publish once per tick; teardown terminates the worker and revokes its URL', async () => {
	const realCreate = URL.createObjectURL;
	const realRevoke = URL.revokeObjectURL;
	const realWorker = globalThis.Worker;
	const blobs = new Map();
	const revoked = [];
	const workers = [];
	URL.createObjectURL = (blob) => {
		const url = `blob:test/${blobs.size}`;
		blobs.set(url, blob);
		return url;
	};
	URL.revokeObjectURL = (url) => revoked.push(url);
	globalThis.Worker = class {
		constructor(url) {
			this.url = url;
			this.terminated = false;
			this.onmessage = null;
			workers.push(this);
		}
		terminate() {
			this.terminated = true;
		}
	};
	try {
		let published = 0;
		const stop = ticker.startMirrorPublishTicker(() => {
			published += 1;
		});
		assert.equal(workers.length, 1, 'exactly one Worker is created');
		const [worker] = workers;
		assert.ok(blobs.has(worker.url), 'the Worker loads the blob URL the ticker created');

		// Run the REAL worker source against doubles of the worker global scope.
		const source = await blobs.get(worker.url).text();
		const intervals = [];
		new Function('setInterval', 'postMessage', source)(
			(fn, ms) => intervals.push({ fn, ms }),
			(data) => worker.onmessage({ data })
		);
		assert.deepEqual(
			intervals.map((i) => i.ms),
			[ticker.MIRROR_PUBLISH_INTERVAL_MS],
			'the worker arms one interval at the publish cadence'
		);
		intervals[0].fn();
		intervals[0].fn();
		assert.equal(published, 2, 'each worker tick publishes once');
		assert.equal(worker.terminated, false);
		assert.deepEqual(revoked, []);

		stop();
		assert.equal(worker.terminated, true, 'teardown terminates the worker');
		assert.deepEqual(revoked, [worker.url], 'teardown revokes the blob URL');
	} finally {
		URL.createObjectURL = realCreate;
		URL.revokeObjectURL = realRevoke;
		globalThis.Worker = realWorker;
	}
});
