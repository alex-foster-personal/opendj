/**
 * Plain-word job labels for the jobs drawer (src/lib/rb/job-labels.ts), issue #9655.
 * The drawer row test renders the real JobsDrawer through svelte/server.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO_ROOT = new URL('../../../../../', import.meta.url);

const DRAWER_ENTRY = [
	"export { default as JobsDrawer } from '$lib/components/rb/JobsDrawer.svelte';",
	"export { jobsStore } from '$lib/rb/jobs-store.svelte';",
	"export { capabilities } from '$lib/api/capabilities.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let labels;
let drawerMod;

before(async () => {
	[labels, drawerMod] = await Promise.all([
		loadTypeScriptModule('src/lib/rb/job-labels.ts'),
		loadSvelteSsrModule(DRAWER_ENTRY)
	]);
});

function withConsoleErrors(fn) {
	const calls = [];
	const original = console.error;
	console.error = (...args) => calls.push(args);
	try {
		return { result: fn(), calls };
	} finally {
		console.error = original;
	}
}

/** Resolve every register_worker(...) site in engine Python to its kind string. */
function engineRegisteredKinds() {
	const read = (rel) => readFileSync(new URL(rel, REPO_ROOT), 'utf8');
	const constant = (src, name) => {
		const m = src.match(new RegExp(`^${name}(?:\\s*:\\s*str)?\\s*=\\s*"([^"]+)"`, 'm'));
		assert.ok(m, `constant ${name} not found`);
		return m[1];
	};
	const app = read('apps/engine_core/app.py');
	const setup = read('apps/engine_core/setup/jobs.py');
	const sites = [...app.matchAll(/register_worker\((\w+)\.JOB_KIND/g)].map((m) => m[1]);
	const moduleFor = {
		stems_job: 'apps/stems/job.py',
		cloud_job: 'apps/cloud/job.py',
		anlz_cache_job: 'apps/analysis/anlz_cache_job.py'
	};
	const kinds = sites.map((alias) => {
		assert.ok(moduleFor[alias], `unmapped register_worker alias ${alias} in app.py`);
		return constant(read(moduleFor[alias]), 'JOB_KIND');
	});
	assert.match(setup, /register_worker\(SETUP_IMPORT_KIND,/);
	kinds.push(constant(setup, 'SETUP_IMPORT_KIND'));
	return kinds.sort();
}

test('folder import reads "Import music folder"', () => {
	const job = { kind: 'setup.import-rekordbox', payload: { mode: 'folder' } };
	assert.equal(labels.jobDisplayLabel(job), 'Import music folder');
});

test('rekordbox import, and the engine default of no mode, read "Import rekordbox library"', () => {
	for (const payload of [{ mode: 'rekordbox' }, {}]) {
		assert.equal(
			labels.jobDisplayLabel({ kind: 'setup.import-rekordbox', payload }),
			'Import rekordbox library'
		);
	}
});

test('every table label is plain words with no dots', () => {
	for (const kind of labels.ENGINE_JOB_KINDS) {
		const label = labels.jobDisplayLabel({ kind, payload: {} });
		assert.notEqual(label, kind);
		assert.doesNotMatch(label, /\./);
	}
});

test('unknown kind falls back to raw kind and logs once', () => {
	const { calls } = withConsoleErrors(() => {
		assert.equal(labels.jobDisplayLabel({ kind: 'new.kind', payload: {} }), 'new.kind');
		assert.equal(labels.jobDisplayLabel({ kind: 'new.kind', payload: {} }), 'new.kind');
	});
	assert.equal(calls.length, 1);
});

test('prototype names never resolve to a label', () => {
	const { result } = withConsoleErrors(() =>
		labels.jobDisplayLabel({ kind: 'toString', payload: {} })
	);
	assert.equal(result, 'toString');
});

test('every kind the engine registers has a label row', () => {
	assert.deepEqual(engineRegisteredKinds(), [...labels.ENGINE_JOB_KINDS].sort());
	assert.deepEqual(Object.keys(labels.JOB_LABELS).sort(), [...labels.ENGINE_JOB_KINDS].sort());
});

/** Minimal JobOut row for SSR drawer rendering. */
function setupImportRow(id, mode, status, progress) {
	return {
		id,
		kind: 'setup.import-rekordbox',
		payload: { mode },
		status,
		progress,
		attempt: 1,
		created_at: '2026-11-08T12:00:00.000+00:00',
		started_at: '2026-11-08T12:00:30.000+00:00',
		finished_at: status === 'succeeded' ? '2026-11-08T12:05:00.000+00:00' : null,
		message: null,
		error: null,
		owner_boot_id: 'boot-test',
		owner_pid: null,
		worker_pid: null,
		worker_pgid: null,
		worker_argv: null,
		worker_started_at: null,
		external_ref: null
	};
}

test('drawer keeps raw kind in title and data-agent-kind', () => {
	const folderRow = setupImportRow('job-folder', 'folder', 'running', 0.5);
	const rekordboxRow = setupImportRow('job-rb', 'rekordbox', 'succeeded', 1);
	const priorFlavor = drawerMod.capabilities.flavor;
	try {
		drawerMod.capabilities.flavor = 'engine';
		drawerMod.jobsStore.drawerOpen = true;
		drawerMod.jobsStore.jobs = [folderRow, rekordboxRow];
		const html = drawerMod.render(drawerMod.JobsDrawer).body;
		const visible = html
			.replace(/<[^>]*>/g, ' ')
			.replace(/\s+/g, ' ')
			.trim();
		assert.match(visible, /Import music folder/);
		assert.match(visible, /Import rekordbox library/);
		assert.doesNotMatch(visible, /setup\.import-rekordbox/);
		const rawKind = 'data-agent-kind="setup.import-rekordbox"';
		assert.equal(html.split(rawKind).length - 1, 2, 'data-agent-kind on each row');
		assert.match(html, /title="job kind: setup\.import-rekordbox \(id job-folder\)"/);
		assert.match(html, /title="job kind: setup\.import-rekordbox \(id job-rb\)"/);
		assert.match(html, /aria-label="Import music folder progress"/);
	} finally {
		drawerMod.jobsStore.jobs = [];
		drawerMod.jobsStore.drawerOpen = false;
		drawerMod.capabilities.flavor = priorFlavor;
	}
});
