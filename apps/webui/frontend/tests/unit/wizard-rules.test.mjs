/**
 * Step announcements and the once-only polite announcer rule for the first-run wizard.
 *
 * Regression lines:
 * - if stepAnnouncement does not match STEP_TITLES and stepPosition/stepCount -> broken
 * - if wizardAnnouncement re-announces the same refusal on re-render -> broken
 * - if a step change is drowned by a refusal sentence instead of the step title -> broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/setup/wizard-rules.ts');
});

// REQ: SETUP-12014
test('stepAnnouncement names the step title and position in the branch', () => {
	assert.equal(mod.stepAnnouncement('detect', 'folder'), 'Find your music, step 2 of 5');
	assert.equal(mod.stepAnnouncement('confirm', 'rekordbox'), 'Confirm the import, step 3 of 6');
	assert.equal(mod.stepAnnouncement('welcome', null), 'Welcome, step 1 of 5');
	assert.equal(mod.stepAnnouncement('done', 'rekordbox'), 'Done, step 6 of 6');
});

test('wizardAnnouncement speaks step changes and new refusals once', () => {
	const refuse = 'Choose a folder first.';
	assert.equal(
		mod.wizardAnnouncement(null, { step: 'welcome', source: null, refusal: null }),
		'Welcome, step 1 of 5'
	);
	assert.equal(
		mod.wizardAnnouncement(
			{ step: 'welcome', source: null, refusal: null },
			{ step: 'detect', source: null, refusal: refuse }
		),
		'Find your music, step 2 of 5'
	);
	assert.equal(
		mod.wizardAnnouncement(
			{ step: 'detect', source: 'folder', refusal: null },
			{ step: 'detect', source: 'folder', refusal: refuse }
		),
		refuse
	);
	assert.equal(
		mod.wizardAnnouncement(
			{ step: 'detect', source: 'folder', refusal: refuse },
			{ step: 'detect', source: 'folder', refusal: refuse }
		),
		null
	);
	assert.equal(
		mod.wizardAnnouncement(
			{ step: 'detect', source: 'folder', refusal: refuse },
			{ step: 'detect', source: 'rekordbox', refusal: refuse }
		),
		null
	);
	assert.equal(
		mod.wizardAnnouncement(
			{ step: 'detect', source: 'folder', refusal: refuse },
			{ step: 'detect', source: 'folder', refusal: null }
		),
		null
	);
	assert.equal(
		mod.wizardAnnouncement(
			{ step: 'detect', source: 'folder', refusal: refuse },
			{ step: 'detect', source: 'folder', refusal: 'Pick a readable folder first.' }
		),
		'Pick a readable folder first.'
	);
	assert.equal(
		mod.wizardAnnouncement(
			{ step: 'detect', source: null, refusal: null },
			{ step: 'welcome', source: null, refusal: null }
		),
		'Welcome, step 1 of 5'
	);
});
