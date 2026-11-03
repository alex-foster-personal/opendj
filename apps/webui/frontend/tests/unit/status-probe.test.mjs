/**
 * createAssistantStatusProbe re-runs GET /api/v1/assistant/status when the sidebar opens.
 */
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const originalFetch = globalThis.fetch;
after(() => {
	globalThis.fetch = originalFetch;
});

const ENTRY = [
	"export { createAssistantStatusProbe } from '$lib/assistant/status-probe.svelte';",
	"import { flushSync } from 'svelte';",
	"import { createAssistantStatusProbe } from '$lib/assistant/status-probe.svelte';",
	'export function mountStatusHarness(initialOpen = false) {',
	'	let open = $state(initialOpen);',
	'	let probe: ReturnType<typeof createAssistantStatusProbe> | null = null;',
	'	const dispose = $effect.root(() => {',
	'		probe = createAssistantStatusProbe(() => open);',
	'	});',
	'	flushSync();',
	'	return {',
	'		get probe() {',
	'			return probe!;',
	'		},',
	'		setOpen(value: boolean) {',
	'			open = value;',
	'			flushSync();',
	'		},',
	'		stop() {',
	'			dispose();',
	'		}',
	'	};',
	'}'
].join('\n');

const STATUS_PATH = '/api/v1/assistant/status';

function statusOk() {
	return new Response(
		JSON.stringify({ configured: true, model: 'google/gemini-3.7-flash', mode: 'byok' }),
		{ status: 200, headers: { 'content-type': 'application/json' } }
	);
}

function statusCalls(calls) {
	return (url) => {
		calls.push(String(url));
		if (String(url).includes(STATUS_PATH)) return statusOk();
		return new Response('not found', { status: 404 });
	};
}

test('if the sidebar opens once then closes then reopens then status is fetched twice not once then broken', async () => {
	const live = await loadRuneModule(ENTRY);
	const calls = [];
	globalThis.fetch = statusCalls(calls);
	const harness = live.mountStatusHarness(true);
	try {
		assert.equal(calls.filter((u) => u.includes(STATUS_PATH)).length, 1);
		harness.setOpen(false);
		assert.equal(calls.filter((u) => u.includes(STATUS_PATH)).length, 1);
		harness.setOpen(true);
		assert.equal(calls.filter((u) => u.includes(STATUS_PATH)).length, 2);
	} finally {
		harness.stop();
	}
});

test('if reopen after a failed probe does not clear statusError before retry then broken', async () => {
	const live = await loadRuneModule(ENTRY);
	let attempt = 0;
	globalThis.fetch = (url) => {
		if (!String(url).includes(STATUS_PATH)) return new Response('not found', { status: 404 });
		attempt += 1;
		if (attempt === 1) return new Response('bad', { status: 502 });
		return statusOk();
	};
	const harness = live.mountStatusHarness(true);
	try {
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.ok(harness.probe.statusError !== null, 'first probe must fail');
		harness.setOpen(false);
		harness.setOpen(true);
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(harness.probe.statusError, null);
		assert.equal(harness.probe.status?.mode, 'byok');
	} finally {
		harness.stop();
	}
});

test('if closing the sidebar lets a late status rejection set statusError then broken', async () => {
	const live = await loadRuneModule(ENTRY);
	let rejectLate = () => {};
	globalThis.fetch = (url) => {
		if (!String(url).includes(STATUS_PATH)) return new Response('not found', { status: 404 });
		return new Promise((_resolve, reject) => {
			rejectLate = () => reject(new Error('late failure after close'));
		});
	};
	const harness = live.mountStatusHarness(true);
	try {
		harness.setOpen(false);
		await new Promise((resolve) => setTimeout(resolve, 0));
		rejectLate();
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(harness.probe.statusError, null);
	} finally {
		harness.stop();
	}
});
