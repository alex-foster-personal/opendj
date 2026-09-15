/**
 * Fresh-install orchestration helpers (issue #2722).
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

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/preflight/fresh-install.ts', {
		viteApiBase: 'https://fresh-install-orchestration.example.test'
	});
});

test('needsSetupForEmptyLibrary when engine pass and library fail', () => {
	const checks = [
		check(),
		check({
			id: 'library-attached',
			label: 'Library attached',
			status: 'fail',
			detail: '0 tracks'
		})
	];
	assert.equal(mod.needsSetupForEmptyLibrary(checks, false), true);
	assert.equal(mod.needsSetupForEmptyLibrary(checks, true), false);
});

test('needsSetupForEmptyLibrary ignores healthy library', () => {
	const checks = [
		check(),
		check({
			id: 'library-attached',
			label: 'Library attached',
			status: 'pass',
			detail: '42 tracks'
		})
	];
	assert.equal(mod.needsSetupForEmptyLibrary(checks, false), false);
});
