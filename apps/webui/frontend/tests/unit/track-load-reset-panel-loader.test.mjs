/**
 * requirement: PERF-BOOT-11728
 *
 * The /performance boot bundle does not carry TrackLoadResetPanel: TrackLoadResetPanelLoader
 * fetches it by dynamic import() on the first RST open, once per document, and shows a failed
 * fetch inline (never a toast, never a same-document retry).
 *
 * regression: +page.svelte statically imports TrackLoadResetPanel.svelte again
 * regression: the loader retries a rejected import in the same document
 * regression: a failed import shows nothing, or a toast, instead of inline error text
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(new URL(`../../${p}`, import.meta.url), 'utf8');
const page = read('src/routes/performance/+page.svelte');
const loader = read('src/lib/components/rb/TrackLoadResetPanelLoader.svelte');

const staticImportOf = (file) => new RegExp(`import\\s+\\w+\\s+from\\s+['"][^'"]*${file}['"]`);
const dynamicImportOf = (file) => new RegExp(`import\\(\\s*['"][^'"]*${file}['"]\\s*\\)`, 'g');

let lazy;
let ssr;

before(async () => {
	lazy = await loadTypeScriptModule('src/lib/components/rb/lazy-once-import.ts');
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	ssr = await loadSvelteSsrModule(
		[
			"export { default as TrackLoadResetPanelLoader } from '$lib/components/rb/TrackLoadResetPanelLoader.svelte';",
			"export { default as TrackLoadResetPanel } from '$lib/components/rb/TrackLoadResetPanel.svelte';",
			"export { openTrackLoadReset, closeTrackLoadReset, trackLoadResetOpen } from '$lib/rb/track-load-reset.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

test('/performance mounts the reset panel through the loader, never statically', () => {
	assert.doesNotMatch(page, staticImportOf('TrackLoadResetPanel\\.svelte'));
	assert.match(page, staticImportOf('TrackLoadResetPanelLoader\\.svelte'));
	assert.match(page, /<TrackLoadResetPanelLoader \/>/);
	assert.doesNotMatch(page, /<TrackLoadResetPanel \/>/);
});

test('the loader imports the panel only inside a function, once', () => {
	assert.doesNotMatch(loader, staticImportOf('TrackLoadResetPanel\\.svelte'));
	assert.equal(loader.match(dynamicImportOf('TrackLoadResetPanel\\.svelte'))?.length, 1);
	assert.match(
		loader,
		/\(\)\s*=>\s*import\(\s*['"]\$lib\/components\/rb\/TrackLoadResetPanel\.svelte['"]\s*\)/
	);
	assert.match(loader, /trackLoadResetOpen\(\)/);
	assert.doesNotMatch(loader, /pushToast|from\s+['"][^'"]*toast/i);
	assert.equal(loader.match(/role="alert"/g)?.length, 1);
	assert.match(loader, /Reset panel failed to load: \{loadError\}/);
	const markup = loader.slice(loader.lastIndexOf('</script>'));
	assert.doesNotMatch(markup.replace(/<style>[\s\S]*<\/style>/, ''), /reopen|retry/i);
	assert.equal(loader.match(/onclick=\{\(\) => location\.reload\(\)\}>Reload</g)?.length, 1);
	assert.equal(loader.match(/location\.reload\(\)/g)?.length, 1);
});

test('a rejected import shows its error and is never retried in the same document', async () => {
	let calls = 0;
	const errors = [];
	const err = new Error('chunk 404');
	const imp = lazy.createLazyOnceImport(
		() => {
			calls += 1;
			return Promise.reject(err);
		},
		(e) => errors.push(e)
	);
	assert.equal(imp.requested, false);
	assert.equal(calls, 0);
	assert.deepEqual(await imp.request(), { ok: false, error: 'chunk 404' });
	assert.deepEqual(await imp.request(), { ok: false, error: 'chunk 404' });
	assert.deepEqual(await imp.request(), { ok: false, error: 'chunk 404' });
	assert.equal(calls, 1);
	assert.deepEqual(errors, [err]);
	assert.equal(imp.requested, true);
});

test('non-Error rejections and synchronous throws become inline errors', async () => {
	const a = lazy.createLazyOnceImport(() => Promise.reject('boom'));
	assert.deepEqual(await a.request(), { ok: false, error: 'boom' });
	const b = lazy.createLazyOnceImport(() => {
		throw new Error('sync');
	});
	assert.deepEqual(await b.request(), { ok: false, error: 'sync' });
});

test('a resolved import yields the panel once, with the same promise each call', async () => {
	let calls = 0;
	const Sentinel = () => {};
	const imp = lazy.createLazyOnceImport(() => {
		calls += 1;
		return Promise.resolve({ default: Sentinel });
	});
	const first = imp.request();
	const second = imp.request();
	assert.equal(first, second);
	assert.deepEqual(await first, { ok: true, value: Sentinel });
	await imp.request();
	assert.equal(calls, 1);
});

test('rendered: closed loader renders nothing; the panel mounts with its existing (no) props', () => {
	const closed = ssr.render(ssr.TrackLoadResetPanelLoader).body;
	assert.doesNotMatch(closed, /track-load-reset-panel/);

	ssr.openTrackLoadReset('stems', {}, 'A');
	try {
		assert.equal(ssr.trackLoadResetOpen(), true);
		const panel = ssr.render(ssr.TrackLoadResetPanel, { props: {} }).body;
		assert.match(panel, /data-testid="track-load-reset-panel"/);
		assert.match(panel, /Reset when a new track loads/);
		// SSR runs no $effect, so the post-load swap cannot be shown here; the helper tests
		// above cover it. An open loader with nothing loaded yet shows no error box.
		const open = ssr.render(ssr.TrackLoadResetPanelLoader).body;
		assert.doesNotMatch(open, /track-load-reset-panel-load-error/);
	} finally {
		ssr.closeTrackLoadReset();
	}
});
