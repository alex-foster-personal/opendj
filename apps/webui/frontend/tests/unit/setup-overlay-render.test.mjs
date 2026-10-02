/**
 * The first-run overlay, RENDERED (issues #2590 and #3422).
 *
 * setup-overlay.test.mjs pins most of the markup by source shape, which a
 * review of #2612 rightly called insufficient: a regex over the .svelte file
 * passes even when the component never renders the branch, and it cannot see
 * what a server value turns into once it is interpolated. This file compiles
 * the real SetupOverlay with Svelte's server renderer, drives the real wizard
 * store into the confirm, done and error states with deliberately hostile
 * server values (raw home paths, endpoint paths, error codes, library names,
 * env-var names, stage ids, job ids), and reads the markup a user would see.
 *
 * How visible copy is separated from the agent channel: the closed
 * "Details for agents" disclosures and every attribute are removed, and what
 * is left between tags is the text. Each test also asserts the hostile value
 * DID reach the render through the agent channel, so "absent from the copy"
 * is never the absence of a value that was simply not rendered at all.
 *
 * What this cannot prove: reactivity to a later change, clicks, layout. Those
 * stay with the Playwright specs.
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const ENTRY = [
	"export { default as SetupOverlay } from '$lib/components/setup/SetupOverlay.svelte';",
	"export { setupWizard } from '$lib/setup/wizard.svelte';",
	"export { openSetupOverlay, _resetSetupOverlayForTests } from '$lib/setup/overlay.svelte';",
	"export { capabilities } from '$lib/api/capabilities.svelte';",
	"export { jobsStore } from '$lib/rb/jobs-store.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

const FAKE_NAVIGATION = fileURLToPath(new URL('./fake-app-navigation.mjs', import.meta.url));

/** The packaged app's real data dir shape on macOS. */
const DATA_DIR = '/Users/dj/Library/Application Support/com.opendj.desktop';

/**
 * Anything here in VISIBLE copy is the bug. Written independently of
 * present.ts's own containsForbiddenHumanToken, so a hole in that function
 * cannot hide itself from this test.
 */
const FORBIDDEN = [
	['an endpoint path', /\/api\/v1\//],
	['a raw home path', /\/(?:Users|home)\/[^\s<]/],
	['a raw data path', /\/(?:data|tmp|var)\//],
	['an Application Support path', /Application Support/],
	['an error code', /\b[a-z]+(?:_[a-z0-9]+)+\b/],
	['an env var', /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/],
	['a library name', /pyrekordbox|sqlcipher|openrouter/i],
	['a job id', /\bjob-[\w-]+/],
	['an HTTP verb + path', /\b(?:GET|POST) \//]
];

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY, {
		alias: { '$app/navigation': FAKE_NAVIGATION }
	});
});

beforeEach(() => {
	mod._resetSetupOverlayForTests();
	mod.setupWizard._resetForTests();
	mod.capabilities.flavor = 'engine';
	mod.jobsStore.jobs = [];
	mod.openSetupOverlay();
});

function fileProbe(path, exists = true) {
	return { path, exists, size_bytes: exists ? 1024 : null, modified_at: null };
}

function detection(overrides = {}) {
	return {
		installed: true,
		live_db: fileProbe('/Users/dj/Library/Pioneer/rekordbox/master.db'),
		share_dir: fileProbe('/Users/dj/Library/Pioneer/rekordbox/share'),
		working_copy: fileProbe(`${DATA_DIR}/master.db.copy`, false),
		plain_copy: fileProbe(`${DATA_DIR}/master.plain.db`),
		key_available: true,
		key_detail: 'pyrekordbox holds a 64-character key',
		import_source: `${DATA_DIR}/master.plain.db`,
		import_source_encrypted: false,
		blockers: [],
		rekordbox_running: false,
		...overrides
	};
}

function status(overrides = {}) {
	return {
		library_empty: true,
		tracks: 0,
		playlists: 0,
		state_db: fileProbe(`${DATA_DIR}/state/state.db`, false),
		data_dir: DATA_DIR,
		dismissed: false,
		should_show_wizard: true,
		stages: ['detect', 'snapshot', 'decrypt', 'ingest', 'analysis', 'rb_extra_stage'],
		folder_stages: ['detect', 'scan', 'ingest'],
		last_import: null,
		rekordbox: detection(),
		permissions: { all_readable: true, denied: [], roots: [], how_to_grant: '' },
		...overrides
	};
}

/**
 * What a user can read: the text between tags plus every hover title (a
 * tooltip is copy too), with the agent channel removed: no <details>, and no
 * other attribute.
 */
function visibleText(html) {
	const bare = html.replace(/<details\b[\s\S]*?<\/details>/g, '');
	const titles = [...bare.matchAll(/\stitle="([^"]*)"/g)].map((match) => match[1]);
	return [bare, ...titles]
		.join(' ')
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/<details\b[\s\S]*?<\/details>/g, '')
		.replace(/<style\b[\s\S]*?<\/style>/g, '')
		.replace(/<[^>]*>/g, ' ')
		.replace(/&#x2F;|&#47;/g, '/')
		.replace(/\s+/g, ' ')
		.trim();
}

function render() {
	return mod.render(mod.SetupOverlay).body;
}

function assertClean(html, where) {
	const text = visibleText(html);
	assert.ok(text.length > 50, `${where}: the overlay rendered almost nothing: ${text}`);
	for (const [what, pattern] of FORBIDDEN) {
		const hit = pattern.exec(text);
		assert.equal(hit, null, `${where}: visible copy carries ${what}: "${hit?.[0]}" in\n${text}`);
	}
	return text;
}

test('control: the visible-text filter catches a raw value when one is rendered', () => {
	// If visibleText() swallowed everything, every test below would pass on
	// an empty string. This proves the probe can say "dirty".
	const html = '<p>Stored in /Users/dj/Music</p><details><pre>/api/v1/setup</pre></details>';
	assert.throws(() => assertClean(`${html}${'x'.repeat(60)}`, 'control'), /raw home path/);
	// and a hover title counts as copy
	const titled = `<p title="see /api/v1/setup/dismiss">fine</p>${'x'.repeat(60)}`;
	assert.throws(() => assertClean(titled, 'control'), /endpoint path/);
});

test('confirm: stage ids, data dir and source path stay out of the copy', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.detection = detection();
	mod.setupWizard.detectState = 'answered';
	mod.setupWizard.source = 'rekordbox';
	mod.setupWizard.step = 'confirm';

	const html = render();
	const text = assertClean(html, 'confirm');

	assert.match(text, /Confirm the import/);
	// human stage labels are what is read, and an unknown stage is named generically
	assert.match(text, /Read tracks and playlists into the library/);
	assert.match(text, /Another import step/);
	assert.match(text, /Open DJ's library folder/);
	// the agent channel still carries every raw value
	assert.match(html, /data-agent-stage="rb_extra_stage"/);
	assert.match(html, /data-agent-stage="snapshot"/);
	assert.match(html, new RegExp(`data-agent-data-dir="${DATA_DIR}"`));
});

test('confirm: offers an exit other than Back (#3422)', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.detection = detection();
	mod.setupWizard.source = 'rekordbox';
	mod.setupWizard.step = 'confirm';

	const text = visibleText(render());
	assert.match(text, /Continue without importing/);
	assert.match(text, /Start the import/);
});

test('error: a server message full of internals renders as a plain sentence', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.source = 'rekordbox';
	mod.setupWizard.step = 'confirm';
	const raw =
		'cannot import: rekordbox_not_found at /Users/dj/Library/Pioneer ' +
		'(POST /api/v1/setup/import, job-7f3a) set OPENROUTER_API_KEY';
	mod.setupWizard._fail(raw);

	const html = render();
	const text = assertClean(html, 'error');
	assert.match(text, /Something went wrong/);
	// the raw message is still there for an agent, inside the disclosure
	assert.match(html, /Details for agents/);
	assert.ok(html.includes('rekordbox_not_found'), 'agent channel must keep the raw message');
});

test('error: a human server sentence keeps its words, with the path shortened', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.source = 'folder';
	mod.setupWizard.step = 'detect';
	mod.setupWizard._fail('macOS refused to list /Users/dj/Music. Open System Settings');

	const text = assertClean(render(), 'error with grant instructions');
	assert.match(text, /macOS refused to list ~\/Music\. Open System Settings/);
});

test('detect: a failed detection does not render the scanning sentence (#3422)', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.source = 'rekordbox';
	mod.setupWizard.step = 'detect';
	mod.setupWizard.detection = null;
	mod.setupWizard.detectState = 'failed';

	const text = assertClean(render(), 'failed detect');
	assert.doesNotMatch(text, /Looking for your music on this machine/);
	assert.match(text, /did not finish/);

	// control: while it IS scanning, the scanning sentence is what shows
	mod.setupWizard.detectState = 'scanning';
	assert.match(visibleText(render()), /Looking for your music on this machine/);
});

test('detect: a failed Look again over an earlier answer says so (Mac check 3b)', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.source = 'rekordbox';
	mod.setupWizard.step = 'detect';
	mod.setupWizard.detection = detection();
	mod.setupWizard.detectState = 'failed';

	const html = render();
	const text = assertClean(html, 'failed re-check');
	assert.match(text, /Looking again did not finish\. What is shown below is from the earlier search\./);
	assert.match(text, /Your DJ collection: found/, 'the earlier answer may stay visible');
	assert.match(html, /class="probes[^"]*\bstale\b/, 'and it is drawn as the old one');
	assert.doesNotMatch(text, /Looking for your music on this machine/);

	// control: the same answer, not failed, carries no such sentence
	mod.setupWizard.detectState = 'answered';
	const answered = render();
	assert.doesNotMatch(visibleText(answered), /did not finish/);
	assert.doesNotMatch(answered, /class="probes[^"]*\bstale\b/);
});

test('welcome: a failed first status load offers Try again and never strands Get started (Mac check 3c, 4)', async () => {
	const realFetch = globalThis.fetch;
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		if (pathname === '/api/v1/health') return jsonResponse(engineHealth());
		return jsonResponse({ detail: 'the disk said no' }, 500);
	};
	try {
		await mod.setupWizard.load();
	} finally {
		globalThis.fetch = realFetch;
	}
	mod.setupWizard.step = 'welcome';

	const html = render();
	const text = assertClean(html, 'welcome, status failed');
	assert.match(text, /Your library could not be read yet\. Try again, or get started anyway\./);
	assert.match(html, /<button[^>]*data-agent-endpoint="GET \/api\/v1\/setup\/status"[^>]*>\s*Try again/);
	const getStarted = /<button([^>]*)>\s*Get started/.exec(html);
	assert.ok(getStarted !== null, 'Get started must render');
	assert.doesNotMatch(getStarted[1], /disabled/, 'Get started must stay enabled');
	// item 4: the engine's plain-string detail is for agents only
	assert.doesNotMatch(text, /the disk said no/);
	assert.match(html, /data-agent-error="the disk said no"/);
});

test('welcome: a status that loaded offers no Try again (control)', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.detectState = 'answered';
	mod.setupWizard.step = 'welcome';

	const text = visibleText(render());
	assert.doesNotMatch(text, /Try again/);
	assert.doesNotMatch(text, /could not be read yet/);
});

test('done: counts render and the denied roots stay in the agent channel', () => {
	mod.setupWizard.status = status({
		library_empty: false,
		tracks: 1274,
		playlists: 31,
		last_import: {
			kind: 'folder',
			finished_at: '2026-10-01T10:00:00Z',
			started_at: '2026-10-01T09:58:00Z',
			tracks: 1274,
			tracks_written: 1274,
			playlists: 0,
			files_seen: 1300,
			files_dataless: 0,
			files_without_tags: 3,
			files_rejected_unplayable: 0,
			analysis_available: false,
			analysis_detail: 'own_analysis_pending: see /api/v1/analysis/queue',
			roots: ['/Users/dj/Music'],
			unreadable_roots: ['/Users/dj/Music/Locked']
		}
	});
	mod.setupWizard.source = 'folder';
	mod.setupWizard.step = 'done';

	const html = render();
	const text = assertClean(html, 'done');
	assert.match(text, /1274/);
	assert.doesNotMatch(text, /No import was recorded/);
	assert.match(text, /Some folders could not be read/);
	assert.match(html, /data-agent-denied="\/Users\/dj\/Music\/Locked"/);
});

test('done: with no import recorded it says so plainly', () => {
	mod.setupWizard.status = status();
	mod.setupWizard.source = 'rekordbox';
	mod.setupWizard.step = 'done';

	const text = assertClean(render(), 'done, nothing imported');
	assert.match(text, /No import was recorded yet/);
});

test('welcome: the data dir is named, not spelled out', () => {
	mod.setupWizard.status = status({ library_empty: false, tracks: 12 });
	mod.setupWizard.step = 'welcome';

	const html = render();
	assertClean(html, 'welcome');
	assert.match(html, new RegExp(`data-agent-data-dir="${DATA_DIR}"`));
});
