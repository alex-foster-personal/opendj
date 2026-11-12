/**
 * requirement: PERF-BOOT-02
 *
 * The track table's works modals and version stack panel load on first use
 * through the track-table-support seam (#10272, pays back PR #7901).
 *
 * Regression lines:
 *   if track-table-support.ts exports the real WorksModals or VersionStackPanel then broken
 *   if either loader imports its real component other than by import() then broken
 *   if a stackTracks call made before the module lands is dropped then broken
 *   if the module is imported more than once per document then broken
 *   if a null stack id imports the version stack panel then broken
 *   if a failed import renders no role="alert", lacks Reload, or promises a retry then broken
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => fs.readFileSync(path.join(FRONTEND_ROOT, rel), 'utf8');
const BROWSER = 'src/lib/components/rb/browser';

const support = read(`${BROWSER}/track-table-support.ts`);
const worksLoader = read(`${BROWSER}/WorksModalsLoader.svelte`);
const stackLoader = read(`${BROWSER}/VersionStackPanelLoader.svelte`);

const fromReal = (file) => new RegExp(`from\\s+['"][^'"]*/${file}['"]`);
const dynamicImportOf = (file) => new RegExp(`import\\(\\s*['"][^'"]*/${file}['"]\\s*\\)`);

const lazyPanels = await loadTypeScriptModule(`${BROWSER}/track-table-lazy-panels.ts`);
const { firstUseImport, forwardWorksMethods, requestStackPanel } = lazyPanels;

function deferred() {
	let resolve;
	let reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}
const flush = () => new Promise((r) => setTimeout(r, 0));

test('the seam exports the two loaders under the old names', () => {
	assert.match(
		support,
		/export \{ default as WorksModals \} from '\.\/WorksModalsLoader\.svelte';/
	);
	assert.match(
		support,
		/export \{ default as VersionStackPanel \} from '\.\/VersionStackPanelLoader\.svelte';/
	);
	assert.doesNotMatch(support, fromReal('WorksModals\\.svelte'));
	assert.doesNotMatch(support, fromReal('VersionStackPanel\\.svelte'));
});

test('neither loader statically imports its real component', () => {
	for (const [src, file] of [
		[worksLoader, 'WorksModals\\.svelte'],
		[stackLoader, 'VersionStackPanel\\.svelte']
	]) {
		assert.doesNotMatch(src, fromReal(file));
		assert.match(src, dynamicImportOf(file));
		assert.match(src, /role="alert"/);
		assert.match(src, /location\.reload\(\)/);
		const alert = src.slice(
			src.indexOf('role="alert"'),
			src.indexOf('{/if}', src.indexOf('role="alert"'))
		);
		assert.doesNotMatch(alert, /retry|try again/i);
	}
});

test('no other source file statically imports either real component', () => {
	const offenders = [];
	const walk = (dir) => {
		for (const entry of fs.readdirSync(path.join(FRONTEND_ROOT, dir), { withFileTypes: true })) {
			const rel = `${dir}/${entry.name}`;
			if (entry.isDirectory()) walk(rel);
			else if (/\.(svelte|ts|js)$/.test(entry.name)) {
				const src = read(rel);
				if (
					fromReal('WorksModals\\.svelte').test(src) ||
					fromReal('VersionStackPanel\\.svelte').test(src)
				)
					offenders.push(rel);
			}
		}
	};
	walk('src');
	assert.deepEqual(offenders, []);
});

test('a works call before the module lands imports once and is replayed with its arguments', async () => {
	let imports = 0;
	const gate = deferred();
	const lazy = firstUseImport(() => {
		imports += 1;
		return gate.promise;
	});
	const calls = [];
	const fake = {
		async stackTracks(ids, title) {
			calls.push([ids, title]);
		}
	};
	const forward = forwardWorksMethods(async () => {
		await lazy.load();
		return fake;
	});
	const ids = ['a', 'b'];
	const first = forward.stackTracks(ids, 'T');
	const second = forward.stackTracks(['a', 'b'], 'T');
	await flush();
	assert.equal(calls.length, 0);
	assert.equal(lazy.started, true);
	gate.resolve({ default: {} });
	await Promise.all([first, second]);
	assert.equal(imports, 1);
	assert.deepEqual(calls, [
		[['a', 'b'], 'T'],
		[['a', 'b'], 'T']
	]);
	assert.equal(calls[0][0], ids);
	// CONTROL: after landing, a call forwards straight away without a new import.
	await forward.stackTracks(['c'], 'U');
	assert.equal(imports, 1);
	assert.deepEqual(calls[2], [['c'], 'U']);
});

test('a failed import is tried once per document and the call rejects', async () => {
	let imports = 0;
	const lazy = firstUseImport(() => {
		imports += 1;
		return Promise.reject(new Error('chunk gone'));
	});
	await assert.rejects(lazy.load(), /chunk gone/);
	assert.equal(lazy.failure, 'chunk gone');
	const again = lazy.load();
	await assert.rejects(again, /chunk gone/);
	assert.equal(imports, 1);
	let forwarded = 0;
	const forward = forwardWorksMethods(async () => {
		await lazy.load();
		return {
			async stackTracks() {
				forwarded += 1;
			}
		};
	});
	await assert.rejects(forward.stackTracks(['a'], 'T'), /chunk gone/);
	assert.equal(forwarded, 0);
});

test('the version stack loader imports nothing while the stack id is null', async () => {
	let imports = 0;
	const lazy = firstUseImport(async () => {
		imports += 1;
		return { default: {} };
	});
	requestStackPanel(null, lazy);
	requestStackPanel(null, lazy);
	requestStackPanel(null, lazy);
	await flush();
	assert.equal(imports, 0);
	assert.equal(lazy.started, false);
	// CONTROL: a non-null id imports, once.
	requestStackPanel('x', lazy);
	requestStackPanel('y', lazy);
	await flush();
	assert.equal(imports, 1);
	// Source: the panel only mounts, and load() only runs, for a non-null id.
	assert.match(stackLoader, /\{#if stableId !== null && Panel\}/);
	assert.match(
		stackLoader,
		/if \(stableId === null \|\| Panel !== null\) return;\s*requestStackPanel/
	);
});
