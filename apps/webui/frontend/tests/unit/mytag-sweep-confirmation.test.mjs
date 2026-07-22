import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const confirmation = await loadTypeScriptModule('src/lib/rb/mytag-sweep-confirmation.ts');

test('cancelled MyTag sweep makes no API call', async () => {
	let apiCalls = 0;
	const confirmed = await confirmation.runConfirmedMyTagSweep(
		{ action: 'delete', tagName: 'warmup', affectedTrackCount: 3 },
		() => false,
		async () => {
			apiCalls += 1;
		}
	);

	assert.equal(confirmed, null);
	assert.equal(apiCalls, 0);
});

test('confirmation names the tag and exact affected count before calling the API', async () => {
	let prompt = '';
	let apiCalls = 0;
	const confirmed = await confirmation.runConfirmedMyTagSweep(
		{ action: 'rename', tagName: 'warmup', affectedTrackCount: 3, destinationName: 'opening' },
		(message) => {
			prompt = message;
			return true;
		},
		async () => {
			apiCalls += 1;
			return 'called';
		}
	);

	assert.equal(confirmed, 'called');
	assert.match(prompt, /warmup/);
	assert.match(prompt, /3 track\(s\)/);
	assert.match(prompt, /opening/);
	assert.equal(apiCalls, 1);
});
