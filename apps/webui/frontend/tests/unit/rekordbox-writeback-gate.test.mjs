/**
 * One-way import mode in the UI: every rekordbox-write control is inert.
 *
 * Two halves, for the same reason capability-gating-markup.test.mjs splits:
 * this harness is node:test + esbuild with no component mount infra, so the
 * store is tested by EXECUTION and the .svelte files by source shape.
 *
 * The executable half is the real module over a stubbed globalThis.fetch. The
 * markup half pins the four facts a refactor silently breaks: the control is
 * `disabled`, it carries the refusal as its `title`, the handler returns early
 * so no request is fired even if the disabled attribute is lost, and the page
 * probes the gate at mount.
 *
 * Regression lines:
 * - if the refusal is null before any probe resolves then a control renders
 *   live against a daemon that has not said writes are allowed
 * - if a failed probe is memoized then a daemon that came up late stays locked out
 * - if a second probe() re-requests after a success then it is a poll, not a probe
 * - if the apply/rollback/relocate handler stops returning early then a lost
 *   `disabled` attribute becomes a live request against the real library
 * - if the tooltip drifts from UI_REFUSAL_TITLE in
 *   apps/shared/rekordbox_writeback.py then the UI and the server disagree
 * - if the refusal is reworded as PARITY-TODO then "not built" and "switched
 *   off on purpose" have been collapsed into one wrong sentence
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://writeback-gate.example.test';
const GATE_PATH = '/api/v1/rekordbox/writeback-gate';
const REFUSAL = 'sync to rekordbox disabled - one-way import only';
const PARITY_TODO = 'not implemented - see PARITY-TODO';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

function readRepo(relative) {
	return readFileSync(fileURLToPath(new URL(`../../../../../${relative}`, import.meta.url)), 'utf8');
}

let mod;
let gate;
let originalFetch;
let requested;

function gateBody(overrides = {}) {
	return {
		enabled: false,
		code: 'rekordbox_writeback_disabled',
		message: 'sync to rekordbox is disabled: one-way import mode',
		ui_title: REFUSAL,
		env_var: 'MDT_REKORDBOX_WRITEBACK_ENABLED',
		surfaces: [],
		...overrides
	};
}

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/rekordbox-writeback.svelte.ts', {
		viteApiBase: API_BASE
	});
	gate = mod.rekordboxWriteback;
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	gate._resetForTests();
	requested = [];
	globalThis.fetch = async (request) => {
		requested.push(new URL(request.url).pathname);
		return jsonResponse(gateBody());
	};
});

// ------------------------------------------------------------- fail closed

test('the refusal is non-null before any probe resolves', () => {
	assert.equal(mod.rekordboxWritebackRefusal(), REFUSAL);
	assert.equal(gate.enabled, false);
	assert.equal(requested.length, 0, 'reading the refusal must not fire a request');
});

test('a daemon that answers enabled:false keeps every control inert', async () => {
	await gate.probe();
	assert.equal(gate.enabled, false);
	assert.equal(mod.rekordboxWritebackRefusal(), REFUSAL);
	assert.deepEqual(requested, [GATE_PATH]);
});

test('a failed probe stays refused and is retried, not memoized', async () => {
	globalThis.fetch = async () => {
		requested.push(GATE_PATH);
		throw new Error('daemon not up yet');
	};
	await gate.probe();
	assert.equal(mod.rekordboxWritebackRefusal(), REFUSAL);
	assert.match(gate.error, /daemon not up yet/);
	await gate.probe();
	assert.equal(requested.length, 2, 'a failed probe must be retried by the next caller');
});

test('a 500 from the gate route leaves the UI refused', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { code: 'boom', message: 'no' } }), {
			status: 500,
			headers: { 'content-type': 'application/json' }
		});
	await gate.probe();
	assert.equal(mod.rekordboxWritebackRefusal(), REFUSAL);
});

// ------------------------------------------------------------ enabled path

test('only an explicit enabled:true clears the refusal', async () => {
	globalThis.fetch = async (request) => {
		requested.push(new URL(request.url).pathname);
		return jsonResponse(gateBody({ enabled: true }));
	};
	await gate.probe();
	assert.equal(gate.enabled, true);
	assert.equal(mod.rekordboxWritebackRefusal(), null);
});

test('the probe is memoized once it succeeds', async () => {
	await gate.probe();
	await gate.probe();
	await gate.probe();
	assert.equal(requested.length, 1, 'one probe, not a poll');
});

test('the tooltip comes from the daemon, never a second hardcoded copy', async () => {
	globalThis.fetch = async () => jsonResponse(gateBody({ ui_title: 'server said this' }));
	await gate.probe();
	assert.equal(mod.rekordboxWritebackRefusal(), 'server said this');
});

// ------------------------------------------------- one wording, both sides

test('the fallback wording matches UI_REFUSAL_TITLE in the python gate', () => {
	const python = readRepo('apps/shared/rekordbox_writeback.py');
	assert.match(python, new RegExp(`UI_REFUSAL_TITLE = "${REFUSAL}"`));
	assert.equal(mod.WRITEBACK_DISABLED_TITLE, REFUSAL);
});

test('the refusal is not the PARITY-TODO stub wording', () => {
	// "not built" and "built, and switched off on purpose" are different facts.
	// The module docstring names PARITY-TODO on purpose to say so; what must
	// never happen is the two collapsing into one string a control renders.
	assert.notEqual(REFUSAL, PARITY_TODO);
	assert.notEqual(mod.WRITEBACK_DISABLED_TITLE, PARITY_TODO);
	assert.equal(
		read('src/lib/rb/rekordbox-writeback.svelte.ts').includes(`'${PARITY_TODO}'`),
		false,
		'the gate module must never define the stub wording as a value'
	);
});

// ------------------------------------------------------------- markup half

const writebackPage = read('src/routes/playlist/[id]/writeback/+page.svelte');
const reconcilePage = read('src/routes/reconcile/+page.svelte');

test('the writeback page probes the gate at mount', () => {
	assert.match(writebackPage, /void rekordboxWriteback\.probe\(\)/);
});

test('the reconcile page probes the gate at mount', () => {
	assert.match(reconcilePage, /void rekordboxWriteback\.probe\(\)/);
});

test('apply and rollback are disabled by the refusal and titled with it', () => {
	// `[^>]*` cannot be used to walk to the next attribute: the disabled
	// expression itself contains `>` (plan.unresolved.length > 0).
	assert.match(
		writebackPage,
		/onclick=\{onApply\}[\s\S]{0,300}?disabled=\{[\s\S]{0,200}?writebackRefusal !== null\}/
	);
	assert.match(writebackPage, /onclick=\{onApply\}[\s\S]{0,300}?title=\{writebackRefusal \?\?/);
	assert.match(
		writebackPage,
		/onclick=\{onRollback\}[\s\S]{0,300}?disabled=\{[\s\S]{0,200}?writebackRefusal !== null\}/
	);
	assert.match(writebackPage, /onclick=\{onRollback\}[\s\S]{0,300}?title=\{writebackRefusal \?\?/);
});

test('the writeback handlers refuse before firing, not just visually', () => {
	assert.match(
		writebackPage,
		/async function onApply\(\): Promise<void> \{\s*\n\s*if \(writebackRefusal !== null\) return;/
	);
	assert.match(
		writebackPage,
		/async function onRollback\(\): Promise<void> \{\s*\n\s*if \(writebackRefusal !== null\) return;/
	);
});

test('the gate is scoped to rekordbox, so djay writeback is untouched', () => {
	assert.match(
		writebackPage,
		/writebackRefusal = \$derived\(selectedVendor === 'rekordbox' \? gateRefusal : null\)/
	);
});

test('the relocate control is disabled by the refusal and titled with it', () => {
	assert.match(reconcilePage, /disabled=\{applying === track\.stable_id \|\| writebackRefusal !== null\}/);
	assert.match(reconcilePage, /title=\{writebackRefusal \?\?/);
	assert.match(
		reconcilePage,
		/async function relocate\([^)]*\): Promise<void> \{\s*\n\s*if \(writebackRefusal !== null\) return;/
	);
});
