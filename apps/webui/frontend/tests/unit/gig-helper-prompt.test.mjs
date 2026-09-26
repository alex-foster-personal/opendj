import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('shouldOfferGigHelper only when gig posture and unset pref', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/gig-helper-prompt.ts');
	assert.equal(mod.shouldOfferGigHelper('gig', 'unset'), true);
	assert.equal(mod.shouldOfferGigHelper('prep', 'unset'), false);
	assert.equal(mod.shouldOfferGigHelper('gig', 'off'), false);
	assert.equal(mod.shouldOfferGigHelper('gig', 'on'), false);
});

test('tryOfferGigHelperPromptOnPostureChange opens only on prep to gig with unset pref', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/gig-helper-prompt.svelte.ts');
	mod.dismissGigHelperPrompt();
	assert.equal(mod.gigHelperPromptVisible, false);
	mod.tryOfferGigHelperPromptOnPostureChange('prep', 'gig', 'unset');
	assert.equal(mod.gigHelperPromptVisible, true);
	mod.dismissGigHelperPrompt();
	mod.tryOfferGigHelperPromptOnPostureChange('gig', 'gig', 'unset');
	assert.equal(mod.gigHelperPromptVisible, false);
	mod.tryOfferGigHelperPromptOnPostureChange('prep', 'gig', 'off');
	assert.equal(mod.gigHelperPromptVisible, false);
});
