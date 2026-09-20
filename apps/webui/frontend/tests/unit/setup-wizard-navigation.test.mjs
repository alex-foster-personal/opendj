/**
 * Forward and back through the first-run wizard.
 *
 * the maintainer, Wed 16 Sep 2026, after walking the first-run flow on demon-llama:
 * "We also can't go forward or back in the first-run setup which is confusing
 * - add forward and back. Block going forward if it's impossible to let user
 * skip a section ofc."
 *
 * Regression lines:
 * - if Back is offered on the welcome step with no reason given then the
 *   first step has a control that silently does nothing -> broken
 * - if Back is offered while an import is actually running then the wizard
 *   walks away from a live job and its "done" screen is a guess -> broken
 * - if Back from the progress step lands on 'confirm' while the FOLDER branch
 *   is selected then the user is shown a rekordbox confirmation screen for an
 *   import that has nothing to do with rekordbox -> broken
 * - if backRefusal and the rendered Back button can disagree then a disabled
 *   control has no explanation attached to it -> broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/setup/wizard.svelte.ts');
});

function ctx(overrides = {}) {
	return {
		source: 'rekordbox',
		detection: null,
		folderRows: [{ id: 'row-1', path: '', scan: null }],
		job: null,
		...overrides
	};
}

// ----------------------------------------------------------------- back

test('Back is refused on the first step, and says so', () => {
	const why = mod.backRefusal('welcome', ctx());
	assert.equal(typeof why, 'string');
	assert.match(why, /first step/i);
});

test('Back is allowed on every step that has one behind it', () => {
	for (const step of ['detect', 'confirm', 'stems', 'done']) {
		assert.equal(mod.backRefusal(step, ctx()), null, `${step} should allow Back`);
	}
});

test('Back is refused while the import is actually running', () => {
	const why = mod.backRefusal('progress', ctx({ job: { id: 'j1', status: 'running', progress: 0.4 } }));
	assert.equal(typeof why, 'string');
	assert.match(why, /running/i);
});

test('Back is allowed again once the import has finished', () => {
	for (const status of ['succeeded', 'failed', 'cancelled']) {
		const why = mod.backRefusal('progress', ctx({ job: { id: 'j1', status, progress: 1 } }));
		assert.equal(why, null, `a ${status} job should not pin the user on the progress step`);
	}
});

test('Back is allowed on progress when no job was ever started', () => {
	// Nothing is running, so nothing can be stranded by leaving.
	assert.equal(mod.backRefusal('progress', ctx({ job: null })), null);
});

// ------------------------------------------------- source-aware sequence

test('the folder branch skips the rekordbox confirm step, forwards and back', () => {
	// 'confirm' is a rekordbox-only screen. Walking a folder import through it
	// shows the user a confirmation for a library they are not importing.
	assert.equal(mod.previousStepFor('progress', 'folder'), 'detect');
	assert.equal(mod.nextStepFor('detect', 'folder'), 'progress');
});

test('control: the rekordbox branch still walks through confirm', () => {
	// Without this, deleting 'confirm' from the list entirely would pass the
	// test above while breaking the main path.
	assert.equal(mod.previousStepFor('progress', 'rekordbox'), 'confirm');
	assert.equal(mod.nextStepFor('detect', 'rekordbox'), 'confirm');
});

test('the ends of the sequence do not run off either edge', () => {
	assert.equal(mod.previousStepFor('welcome', 'rekordbox'), 'welcome');
	assert.equal(mod.nextStepFor('done', 'rekordbox'), 'done');
	assert.equal(mod.previousStepFor('welcome', 'folder'), 'welcome');
	assert.equal(mod.nextStepFor('done', 'folder'), 'done');
});

test('every step is reachable walking forward, and back again', () => {
	for (const source of ['rekordbox', 'folder']) {
		const seen = [];
		let step = 'welcome';
		for (let i = 0; i < 20 && step !== 'done'; i += 1) {
			seen.push(step);
			step = mod.nextStepFor(step, source);
		}
		seen.push(step);
		assert.equal(seen[seen.length - 1], 'done', `${source} never reached done`);
		assert.ok(seen.includes('detect'), `${source} skipped detect`);
		// and the same route runs in reverse
		const back = [];
		let cursor = 'done';
		for (let i = 0; i < 20 && cursor !== 'welcome'; i += 1) {
			back.push(cursor);
			cursor = mod.previousStepFor(cursor, source);
		}
		back.push(cursor);
		assert.deepEqual(back.reverse(), seen, `${source} does not walk back the way it walked forward`);
	}
});

// --------------------------------------------------- the store's methods

test("the store's back() honours the refusal instead of moving anyway", () => {
	mod.setupWizard._resetForTests();
	mod.setupWizard.step = 'welcome';
	mod.setupWizard.back();
	assert.equal(mod.setupWizard.step, 'welcome', 'back() walked off the first step');
});

test("the store's back() follows the source-aware route", () => {
	mod.setupWizard._resetForTests();
	mod.setupWizard.source = 'folder';
	mod.setupWizard.step = 'progress';
	mod.setupWizard.back();
	assert.equal(mod.setupWizard.step, 'detect');
	mod.setupWizard._resetForTests();
});

// ------------------------------------------------------ the step counter

test('the step counter counts only the steps this branch will visit', () => {
	const rb = mod.visibleSteps('rekordbox');
	const folder = mod.visibleSteps('folder');
	assert.ok(rb.includes('confirm'), 'rekordbox must visit confirm');
	assert.ok(!folder.includes('confirm'), 'the folder branch must not count a step it skips');
	assert.equal(mod.stepPosition('detect', 'rekordbox'), 2, 'detect is the 2nd rekordbox step');
	assert.equal(mod.stepPosition('progress', 'folder'), 3, 'progress is the 3rd folder step');
});
