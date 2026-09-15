/**
 * First-run onboarding collision fix: while the setup overlay is open the
 * boot preflight gate must not cover the wizard, and the library-attached
 * row must be hidden because the wizard is resolving it.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function check(overrides = {}) {
	return {
		id: 'engine-alive',
		label: 'Engine alive',
		status: 'pass',
		detail: 'the endpoint answered',
		remediation: null,
		...overrides
	};
}

function libraryAttachedRow(overrides = {}) {
	return check({
		id: 'library-attached',
		label: 'Library attached',
		status: 'fail',
		detail: 'no state.db at /data/state/state.db',
		remediation: 'Run setup to import a library (Cmd+, > Run setup).',
		...overrides
	});
}

let preflightHelpers;

before(async () => {
	preflightHelpers = await loadTypeScriptModule('src/lib/preflight/preflight.svelte.ts', {
		viteApiBase: 'https://preflight-setup-collision.example.test'
	});
});

test('shouldBlockOnPreflight: setup closed and preflight fail blocks', () => {
	assert.equal(preflightHelpers.shouldBlockOnPreflight('fail', false), true);
	assert.equal(preflightHelpers.shouldBlockOnPreflight('unknown', false), true);
});

test('shouldBlockOnPreflight: setup open and preflight fail does not block', () => {
	assert.equal(preflightHelpers.shouldBlockOnPreflight('fail', true), false);
	assert.equal(preflightHelpers.shouldBlockOnPreflight('unknown', true), false);
});

test('shouldBlockOnPreflight: any yielded overlay and preflight fail does not block', () => {
	for (const yieldBootGate of [true]) {
		assert.equal(preflightHelpers.shouldBlockOnPreflight('fail', yieldBootGate), false);
	}
});

test('shouldBlockOnPreflight: a pass never blocks', () => {
	assert.equal(preflightHelpers.shouldBlockOnPreflight('pass', false), false);
	assert.equal(preflightHelpers.shouldBlockOnPreflight('pass', true), false);
});

test('visiblePreflightChecks: setup open excludes library-attached', () => {
	const checks = [check(), libraryAttachedRow()];
	const visible = preflightHelpers.visiblePreflightChecks(checks, true);

	assert.equal(visible.length, 1);
	assert.equal(visible[0].id, 'engine-alive');
});

test('visiblePreflightChecks: setup open still shows other failing checks', () => {
	const checks = [
		check({ id: 'state-db', label: 'State DB', status: 'fail', detail: 'missing' }),
		libraryAttachedRow()
	];
	const visible = preflightHelpers.visiblePreflightChecks(checks, true);

	assert.equal(visible.length, 1);
	assert.equal(visible[0].id, 'state-db');
});

test('visiblePreflightChecks: setup closed includes library-attached', () => {
	const checks = [check(), libraryAttachedRow()];
	const visible = preflightHelpers.visiblePreflightChecks(checks, false);

	assert.equal(visible.length, 2);
	assert.ok(visible.some((row) => row.id === 'library-attached'));
});
