/**
 * PREFLIGHT-05 (the maintainer, Mon 5 Oct 2026): the startup/preflight screen explains
 * its buttons as static text below the "This screen clears itself" footer
 * (no hover tooltips, so nothing moves on hover), and every click shows a
 * visible status built from the action's real result.
 *
 * The markup half renders PreflightActions.svelte and PreflightCheckRow.svelte
 * through the REAL Svelte compiler + SSR renderer (mount-svelte.mjs). The
 * click half drives the injected-dependency runners in preflight-actions.ts,
 * which are exactly what PreflightScreen's onclick calls (PreflightScreen
 * itself cannot be SSR-mounted, see preflight-run-setup.test.mjs).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const FOOTER = 'This screen clears itself automatically once every check passes.';
const AT_1742 = new Date(2026, 9, 5, 17, 42);

let m;

before(async () => {
	m = await bundleSvelteEntry(`
		export { default as PreflightActions } from '$lib/components/preflight/PreflightActions.svelte';
		export { default as PreflightCheckRow } from '$lib/components/preflight/PreflightCheckRow.svelte';
		export * from '$lib/preflight/preflight-actions';
	`);
});

function row(id, label, status) {
	return { id, label, status, detail: `${label} detail`, remediation: null };
}

const MIXED = [
	row('engine-alive', 'Engine alive', 'pass'),
	row('audio-access', 'Audio access', 'fail'),
	row('library-attached', 'Library attached', 'pending')
];

function bootProps(overrides = {}) {
	const actions = ['import', 'recheck', 'permissions'];
	return {
		actions,
		footer: FOOTER,
		explainers: m.preflightExplainers({ actions, runSetup: false }, null),
		...overrides
	};
}

function recorder() {
	const seen = [];
	return { seen, setStatus: (status) => seen.push(status) };
}

// ---------------------------------------------------------------- markup

test('explainers render as static "Label: text" lines below the footer, one per button', async () => {
	const html = await renderToHtml(m.PreflightActions, bootProps());
	const footerAt = html.indexOf(FOOTER);
	const listAt = html.indexOf('data-testid="preflight-explainers"');
	assert.ok(footerAt >= 0, 'footer missing');
	assert.ok(listAt > footerAt, 'explainer list must come after the footer');
	for (const label of ['Import your music', 'Re-check', 'Re-request permissions']) {
		const lineAt = html.indexOf(`<strong>${label}</strong>:`);
		assert.ok(lineAt > footerAt, `no static explainer line for ${label} below the footer`);
	}
});

test('no action button carries a hover title', async () => {
	const html = await renderToHtml(m.PreflightActions, bootProps());
	const buttons = html.match(/<button[^>]*>/g) ?? [];
	assert.equal(buttons.length, 3, 'expected the three boot buttons');
	for (const button of buttons) assert.doesNotMatch(button, /\stitle=/);
});

test('the row Run setup button has no hover title; its explainer is listed instead', async () => {
	const html = await renderToHtml(m.PreflightCheckRow, {
		check: row('library-attached', 'Library attached', 'pending'),
		navigate: () => {}
	});
	const button = html.match(/<button[^>]*data-testid="preflight-run-setup"[^>]*>/);
	assert.ok(button, 'Run setup button missing');
	assert.doesNotMatch(button[0], /\stitle=/);
	const lines = m.preflightExplainers({ actions: ['recheck'], runSetup: true }, null);
	assert.deepEqual(
		lines.map((line) => line.label),
		['Run setup', 'Re-check']
	);
});

test('PreflightScreen delegates its buttons, so no hover title can creep back in there', () => {
	const source = readFileSync(
		new URL('../../src/lib/components/preflight/PreflightScreen.svelte', import.meta.url),
		'utf8'
	);
	assert.doesNotMatch(source, /<button[^>]*\stitle=/);
});

test('a pending click shows a pressed, busy button and the pending status line', async () => {
	const html = await renderToHtml(
		m.PreflightActions,
		bootProps({ pending: 'recheck', status: m.PENDING_STATUS.recheck })
	);
	const button = html.match(/<button[^>]*data-testid="preflight-action-recheck"[^>]*>/)[0];
	assert.match(button, /aria-busy="true"/);
	assert.match(button, /\bpending\b/);
	assert.match(button, /disabled/);
	const statusAt = html.indexOf('data-testid="preflight-action-status"');
	assert.ok(statusAt > html.lastIndexOf('</button>'), 'status line must sit under the buttons');
	assert.ok(html.indexOf('Re-checking...') > statusAt);
});

test('a blocked importer shows its reason as text, never as a tooltip', () => {
	const lines = m.preflightExplainers(
		{ actions: ['import', 'recheck'], runSetup: false },
		'setup is not available in this build'
	);
	assert.equal(lines[0].text, 'unavailable: setup is not available in this build');
	assert.notEqual(lines[1].text, lines[0].text);
});

// ---------------------------------------------------------------- clicks

test('Re-check shows "Re-checking..." then the real pass count and what it is waiting on', async () => {
	const { seen, setStatus } = recorder();
	let calls = 0;
	await m.runRecheck({
		check: async () => {
			calls += 1;
		},
		snapshot: () => ({ checks: MIXED, error: null }),
		now: () => AT_1742,
		setStatus
	});
	assert.equal(calls, 1, 'Re-check must issue the real check');
	assert.deepEqual(seen, [
		'Re-checking...',
		'Checked at 17:42: 1 of 3 checks pass. Still waiting on: Audio access, Library attached.'
	]);
});

test('Re-check against an unreachable engine reports the failure, never a pass', async () => {
	const { seen, setStatus } = recorder();
	await m.runRecheck({
		check: async () => {},
		snapshot: () => ({ checks: MIXED, error: 'fetch failed' }),
		now: () => AT_1742,
		setStatus
	});
	assert.equal(seen.at(-1), 'Checked at 17:42: could not reach the engine (fetch failed).');
});

test('Re-check with every check passing says so with no waiting list', () => {
	const all = [row('a', 'A', 'pass'), row('b', 'B', 'pass')];
	assert.equal(
		m.summarizePreflight({ checks: all, error: null }, AT_1742),
		'Checked at 17:42: 2 of 2 checks pass.'
	);
});

test('Re-request permissions says it asked macOS, where to go next, and the real result', async () => {
	const { seen, setStatus } = recorder();
	await m.runRequestPermissions({
		check: async () => {},
		snapshot: () => ({ checks: MIXED, error: null }),
		now: () => AT_1742,
		setStatus
	});
	assert.equal(seen[0], 'Asking macOS again...');
	assert.match(seen[1], /^Asked macOS again/);
	assert.match(seen[1], /System Settings > Privacy & Security/);
	assert.match(seen[1], /1 of 3 checks pass/);
});

test('Import your music reports it opened the importer, or the real refusal', async () => {
	const ok = recorder();
	await m.runImport(async () => null, ok.setStatus);
	assert.deepEqual(ok.seen, ['Opening the importer...', 'Opened the importer.']);

	const refused = recorder();
	await m.runImport(async () => 'daemon refused setup', refused.setStatus);
	assert.equal(refused.seen.at(-1), 'Could not open the importer: daemon refused setup');
});

test('Retry setup check reports each real outcome', async () => {
	const opened = recorder();
	await m.runRetrySetupCheck(
		{ retry: async () => true, gateError: () => null, open: async () => null },
		opened.setStatus
	);
	assert.equal(opened.seen.at(-1), 'Opened the importer.');

	const notNeeded = recorder();
	await m.runRetrySetupCheck(
		{ retry: async () => false, gateError: () => null, open: async () => null },
		notNeeded.setStatus
	);
	assert.equal(notNeeded.seen.at(-1), 'Setup check answered: no setup needed.');

	const failed = recorder();
	await m.runRetrySetupCheck(
		{ retry: async () => null, gateError: () => 'timed out', open: async () => null },
		failed.setStatus
	);
	assert.equal(failed.seen.at(-1), 'Setup check failed again: timed out');
});
