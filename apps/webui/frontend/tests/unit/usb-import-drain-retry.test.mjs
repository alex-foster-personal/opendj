// requirement: USBIMP-01
// [if] the stick's import succeeds while another analysis drain is running [then] a drain is asked for again until one starts, so the stick's rows are in a drain's backlog [⛔️ if a refused drain (409) is reported as the stick's analysis and no later drain is asked for].
//
// Only the HTTP transport is stubbed: a drain that a running one refuses
// (409) cannot be held open deterministically by the real daemon in
// usb-import-wait.test.mjs. Everything above fetch is the shipped module.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API = 'http://drain-retry.invalid';

function stubFetch(drainStatuses, jobs = {}) {
	const calls = [];
	const original = globalThis.fetch;
	globalThis.fetch = async (url, init = {}) => {
		const request = url instanceof Request ? url : null;
		const path = new URL(request ? request.url : String(url)).pathname;
		const method = request ? request.method : (init.method ?? 'GET');
		calls.push(`${method} ${path}`);
		if (method === 'POST' && path === '/api/v1/analysis-queue/run') {
			const status = drainStatuses.shift() ?? 202;
			return new Response(status === 202 ? '{"status":"running"}' : '{}', { status });
		}
		if (method === 'GET' && path.startsWith('/api/v1/jobs/')) {
			const id = path.slice('/api/v1/jobs/'.length);
			const answer = jobs[id] ?? 'succeeded';
			if (typeof answer === 'number') {
				return new Response(
					JSON.stringify({ detail: { code: 'X', message: `job ${id} answered ${answer}` } }),
					{
						status: answer,
						headers: { 'content-type': 'application/json' }
					}
				);
			}
			return new Response(JSON.stringify({ id, kind: 'setup.import-rekordbox', status: answer }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		throw new Error(`unexpected request ${method} ${path}`);
	};
	return { calls, restore: () => (globalThis.fetch = original) };
}

const usbImport = await loadTypeScriptModule('src/lib/rb/usb-import.ts', { viteApiBase: API });
const job = { id: 'job-1', kind: 'setup.import-rekordbox', status: 'running' };

test('a drain refused because one is running is asked for again until it starts', async () => {
	const stub = stubFetch([409, 409, 202]);
	try {
		await usbImport.analyzeWhenImported(job);
	} finally {
		stub.restore();
	}
	const drains = stub.calls.filter((c) => c === 'POST /api/v1/analysis-queue/run');
	assert.equal(drains.length, 3, JSON.stringify(stub.calls));
	assert.ok(
		stub.calls.indexOf(drains[0]) > stub.calls.indexOf('GET /api/v1/jobs/job-1'),
		JSON.stringify(stub.calls)
	);
});

test('control: a drain that starts at once is asked for exactly once', async () => {
	const stub = stubFetch([202]);
	try {
		await usbImport.analyzeWhenImported(job);
	} finally {
		stub.restore();
	}
	assert.equal(
		stub.calls.filter((c) => c === 'POST /api/v1/analysis-queue/run').length,
		1,
		JSON.stringify(stub.calls)
	);
});

// Sol P1, PR #6223: a reload during the import loses the page's waiter, so the
// job id is remembered and the wait resumes on boot.
function stubStorage(initial) {
	const original = globalThis.localStorage;
	const store = new Map(
		initial === undefined ? [] : [[usbImport.PENDING_IMPORT_KEY, JSON.stringify(initial)]]
	);
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v)),
		removeItem: (k) => store.delete(k)
	};
	return {
		store,
		restore: () => {
			if (original === undefined) delete globalThis.localStorage;
			else globalThis.localStorage = original;
		}
	};
}

test('a remembered import job resumes its analysis wait on boot, then is forgotten', async () => {
	const storage = stubStorage(['job-1']);
	const stub = stubFetch([202]);
	try {
		await usbImport.resumePendingUsbAnalysis();
	} finally {
		stub.restore();
		storage.restore();
	}
	assert.deepEqual(stub.calls, ['GET /api/v1/jobs/job-1', 'POST /api/v1/analysis-queue/run']);
	assert.equal(storage.store.has(usbImport.PENDING_IMPORT_KEY), false);
});

test('control: nothing remembered means nothing is read or drained on boot', async () => {
	const storage = stubStorage(undefined);
	const stub = stubFetch([202]);
	try {
		await usbImport.resumePendingUsbAnalysis();
	} finally {
		stub.restore();
		storage.restore();
	}
	assert.deepEqual(stub.calls, []);
});

test('LibraryNav checks the same key before loading the import module', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(
		new URL('../../src/lib/components/rb/browser/LibraryNav.svelte', import.meta.url),
		'utf8'
	);
	assert.ok(
		src.includes(`localStorage.getItem('${usbImport.PENDING_IMPORT_KEY}')`),
		'LibraryNav reads a different key'
	);
	assert.match(
		src,
		/import\('\$lib\/rb\/usb-import'\)\.then\(\s*\(mod\) => mod\.resumePendingUsbAnalysis\(\)/
	);
});

test('every remembered job resumes, not only the latest', async () => {
	const storage = stubStorage(['job-a', 'job-b']);
	const stub = stubFetch([202, 202]);
	try {
		await usbImport.resumePendingUsbAnalysis();
	} finally {
		stub.restore();
		storage.restore();
	}
	assert.deepEqual(stub.calls.filter((c) => c.startsWith('GET')).sort(), [
		'GET /api/v1/jobs/job-a',
		'GET /api/v1/jobs/job-b'
	]);
	assert.equal(stub.calls.filter((c) => c.startsWith('POST')).length, 2);
	assert.equal(storage.store.has(usbImport.PENDING_IMPORT_KEY), false);
});

test('a job that cannot be read yet stays remembered; one the daemon lost (404) or that failed is dropped', async () => {
	const storage = stubStorage(['flaky', 'gone', 'failed']);
	const stub = stubFetch([], { flaky: 503, gone: 404, failed: 'failed' });
	try {
		await usbImport.resumePendingUsbAnalysis();
	} finally {
		stub.restore();
		storage.restore();
	}
	assert.deepEqual(JSON.parse(storage.store.get(usbImport.PENDING_IMPORT_KEY)), ['flaky']);
	assert.equal(
		stub.calls.filter((c) => c.startsWith('POST')).length,
		0,
		'nothing drains for an unread or failed import'
	);
});

test('a drain that errors leaves the job remembered for the next load', async () => {
	const storage = stubStorage(['job-1']);
	const stub = stubFetch([500]);
	try {
		await usbImport.resumePendingUsbAnalysis();
	} finally {
		stub.restore();
		storage.restore();
	}
	assert.deepEqual(JSON.parse(storage.store.get(usbImport.PENDING_IMPORT_KEY)), ['job-1']);
});

test('a resume chunk that fails to load is said in a toast with Retry', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(
		new URL('../../src/lib/components/rb/browser/LibraryNav.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		src,
		/pushToast\(`Analysis of a stick from before the reload did not resume: \$\{why\}`, 'error'[\s\S]{0,120}label: 'Retry',\s*handler: resumeUsbAnalysisIfPending/
	);
	assert.match(src, /onMount\(\(\) => \{\s*startUsbWatch\(\);\s*resumeUsbAnalysisIfPending\(\);/);
});

// Sol P1s, PR #6223 (b6dec63606): a job that cannot be stored is said at Open,
// an unreadable store is said at boot, and neither wait gives up while the
// page is open.
test('a job that cannot be stored throws, so Open can say a reload would lose it', () => {
	const original = globalThis.localStorage;
	globalThis.localStorage = {
		getItem: () => null,
		setItem: () => {
			throw new Error('quota exceeded');
		},
		removeItem: () => {}
	};
	try {
		assert.throws(() => usbImport.rememberPendingImport('job-1'), /quota exceeded/);
	} finally {
		if (original === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = original;
	}
});

test('control: a job that can be stored is stored once', () => {
	const storage = stubStorage(['job-0']);
	try {
		usbImport.rememberPendingImport('job-1');
		usbImport.rememberPendingImport('job-1');
	} finally {
		storage.restore();
	}
	assert.deepEqual(JSON.parse(storage.store.get(usbImport.PENDING_IMPORT_KEY)), ['job-0', 'job-1']);
});

test('Open says a job it could not store in an error toast, before claiming the wait', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(new URL('../../src/lib/rb/usb-import.ts', import.meta.url), 'utf8');
	assert.match(
		src,
		/try \{\s*rememberPendingImport\(job\.id\);\s*pushToast\('Opened\. Analysis starts when the stick is imported\.', 'info'\);\s*\} catch \(err\) \{\s*pushToast\(`Opened, but the job could not be saved[^`]*`, 'error'\);/
	);
});

test('an unreadable store at boot is said in a toast with Retry, not skipped', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(
		new URL('../../src/lib/components/rb/browser/LibraryNav.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		src,
		/pushToast\(`Sticks opened before the reload could not be read back[^`]*`, 'error'[\s\S]{0,120}label: 'Retry',\s*handler: resumeUsbAnalysisIfPending\s*\}\);\s*return;/
	);
});

test('neither the import wait nor the drain retry has a deadline that drops the analysis', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(new URL('../../src/lib/rb/usb-import.ts', import.meta.url), 'utf8');
	assert.doesNotMatch(src, /return null;|did not start: another analysis is still running/);
	assert.match(src, /async function startDrainAfterActive\(\): Promise<void> \{\s*for \(;;\) \{/);
});

// Sol P1, PR #6223 (765a8af42e): a finished job that cannot be cleared is
// said with a Retry, not only logged, since every reload would resume it.
test('a job that cannot be cleared is said in an error toast with Retry', async () => {
	const original = globalThis.localStorage;
	let removes = 0;
	globalThis.localStorage = {
		getItem: () => JSON.stringify(['job-1']),
		setItem: () => {},
		removeItem: () => {
			removes += 1;
			throw new Error('storage locked');
		}
	};
	try {
		assert.doesNotThrow(() => usbImport.forgetPendingImport('job-1'));
	} finally {
		if (original === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = original;
	}
	assert.equal(removes, 1, 'the clear was not attempted');
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(new URL('../../src/lib/rb/usb-import.ts', import.meta.url), 'utf8');
	assert.match(
		src,
		/export function forgetPendingImport\(jobId: string\): void \{[\s\S]{0,260}\} catch \(err\) \{[\s\S]{0,120}pushToast\(\s*`A finished stick import could not be cleared[\s\S]{0,160}\{ label: 'Retry', handler: \(\) => forgetPendingImport\(jobId\) \}/
	);
});
