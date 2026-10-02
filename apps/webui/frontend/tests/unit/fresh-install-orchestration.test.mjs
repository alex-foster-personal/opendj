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

// The dismissal path. preflight answers 'fail' for an empty library that has NOT
// been dismissed and 'pending' for one that HAS, so 'pending' is the only status
// that can appear after the user clicks "Continue without importing". Until now
// no test used it, which is why the predicate returning true for BOTH went
// unnoticed: dismissing changes the engine's answer without changing the outcome,
// so the wizard reopens on the next poll and the escape hatch does nothing.
function libraryAt(status, detail) {
	return [
		check(),
		check({ id: 'library-attached', label: 'Library attached', status, detail })
	];
}

test('dismissing an empty library must not re-trigger the wizard', () => {
	const beforeDismiss = libraryAt('fail', '0 tracks');
	const afterDismiss = libraryAt('pending', 'setup was dismissed with an empty library');

	// Control: the pre-dismissal state SHOULD raise the wizard. Without this the
	// assertion below could pass on a predicate that is false for everything.
	assert.equal(
		mod.needsSetupForEmptyLibrary(beforeDismiss, false),
		true,
		'an empty, undismissed library should raise the wizard'
	);

	assert.equal(
		mod.needsSetupForEmptyLibrary(afterDismiss, false),
		false,
		'a dismissed empty library must stay dismissed; returning true here means ' +
			'"Continue without importing" is a no-op and the wizard reopens on the next poll'
	);
});

test('a dismissed empty library is distinguishable from an undismissed one', () => {
	// Guards the overshoot in the other direction: a fix that simply drops
	// 'pending' from the predicate would pass the test above while also breaking
	// the undismissed case if the engine ever reported 'pending' for it.
	assert.notEqual(
		mod.needsSetupForEmptyLibrary(libraryAt('fail', '0 tracks'), false),
		mod.needsSetupForEmptyLibrary(libraryAt('pending', 'dismissed'), false),
		'dismissal must change the outcome, not merely the status string'
	);
});

test('a stale fail after the operator closed setup does not auto-open', () => {
	// [if] the operator has closed setup and library-attached is still fail
	// [then] the wizard stays closed, [else stop].
	const fail = libraryAt('fail', '0 tracks');
	assert.equal(mod.shouldAutoOpenEmptyLibrarySetup(fail, false, false), true);
	assert.equal(
		mod.shouldAutoOpenEmptyLibrarySetup(fail, false, true),
		false,
		'closing setup must hold the auto-open across the stale preflight row'
	);
	assert.equal(mod.shouldAutoOpenEmptyLibrarySetup(fail, true, false), false);
});
