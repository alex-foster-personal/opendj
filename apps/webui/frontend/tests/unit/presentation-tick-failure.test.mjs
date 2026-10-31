import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let clock;

before(async () => {
	clock = await loadTypeScriptModule('src/lib/rb/presentation-clock-report.ts');
});

test('a throwing output timestamp is durable public failure state, not a silent freeze', () => {
	clock.clearPresentationTickFailure();
	const error = new Error('timestamp device disappeared');
	clock.notePresentationTickFailure(error);
	assert.equal(
		clock.presentationTickFailure(),
		error.message,
		'if a throwing timestamp leaves no typed failure state then public transport can freeze silently'
	);
	clock.clearPresentationTickFailure();
	assert.equal(clock.presentationTickFailure(), null);
});
