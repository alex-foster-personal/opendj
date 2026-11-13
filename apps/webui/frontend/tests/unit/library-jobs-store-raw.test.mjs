import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"export { libraryJobsStore } from '$lib/rb/library-jobs-store.svelte';",
	"export { jobProgress } from '$lib/rb/job-progress.svelte';",
	"export { capabilities } from '$lib/api/capabilities.svelte';",
	"import { flushSync } from 'svelte';",
	"import { libraryJobsStore } from '$lib/rb/library-jobs-store.svelte';",
	'export function mountStemsReader() {',
	'	let runs = 0;',
	'	const dispose = $effect.root(() => {',
	'		$effect(() => {',
	'			void libraryJobsStore.stems;',
	'			runs += 1;',
	'		});',
	'	});',
	'	flushSync();',
	'	return { get runs() { return runs; }, flush() { flushSync(); }, stop() { dispose(); } };',
	'}'
].join('\n');

const originalFetch = globalThis.fetch;
after(() => {
	globalThis.fetch = originalFetch;
});

test('library jobs arrays are replaced wholesale on hydrate and a reader runs once per hydrate', async () => {
	/** [if] two hydrates land [then] each yields a fresh plain array and the reader effect runs once per hydrate, [else stop]. */
	let n = 0;
	globalThis.fetch = async (request) => {
		const url = typeof request === 'string' ? request : request.url;
		// A legacy daemon: the capability probe clears the library job queue (issue #13222).
		if (url.includes('/api/v1/health')) {
			return new Response(JSON.stringify({ status: 'ok' }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		n += 1;
		const items = [{ stable_id: `s${n}`, lane: 'stems', state: 'running', detail: null }];
		return new Response(JSON.stringify({ items }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	const m = await loadRuneModule(ENTRY);
	m.capabilities._resetForTests();
	await m.capabilities.probe();
	const reader = m.mountStemsReader();
	assert.equal(reader.runs, 1);
	await m.libraryJobsStore.refresh();
	reader.flush();
	const first = m.libraryJobsStore.stems;
	assert.equal(reader.runs, 2);
	assert.ok(Array.isArray(first));
	assert.deepEqual(structuredClone(first), first);
	await m.libraryJobsStore.refresh();
	reader.flush();
	assert.notEqual(m.libraryJobsStore.stems, first);
	assert.equal(reader.runs, 3);
	reader.stop();
	// The hydrated 'running' jobs start jobProgress's tick interval; clear them so the file exits.
	for (let i = 1; i <= n; i += 1) m.jobProgress.clear(`s${i}`, 'stems');
});
