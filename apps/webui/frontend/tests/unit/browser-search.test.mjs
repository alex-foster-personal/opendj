import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('text command search publishes the query to browser subscribers', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');
	const received = [];
	const unsubscribe = search.subscribeBrowserSearch((request) => received.push(request));

	search.requestBrowserSearch('  daft punk  ');
	unsubscribe();
	search.requestBrowserSearch('ignored after unsubscribe');

	assert.deepEqual(received, [
		{ query: '', revision: 0 },
		{ query: 'daft punk', revision: 1 }
	]);
});

test('blank browser searches fail fast', async () => {
	const search = await loadTypeScriptModule('src/lib/rb/browser-search.ts');

	assert.throws(() => search.requestBrowserSearch('   '), /non-empty/);
});
