/**
 * requirement: SET-04
 * [if] publishSetGoalPins stores roles [then] autoPlayOrder.pinRoleOf returns them
 * and clearAutoPlayOrder clears pin roles with the charted order.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const QUEUE_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/autoplay-queue.svelte.ts', import.meta.url)),
	'utf8'
);

let queue;

const ENTRY = [
	"export { autoPlayOrder, publishSetGoalPins, clearAutoPlayOrder } from '$lib/rb/autoplay-queue.svelte';"
].join('\n');

before(async () => {
	queue = await loadRuneModule(ENTRY);
});

test('publishSetGoalPins exposes opener, peak, and closer by stable_id', () => {
	queue.publishSetGoalPins(
		new Map([
			['open-a', 'opener'],
			['peak-b', 'peak'],
			['close-c', 'closer']
		])
	);
	assert.equal(queue.autoPlayOrder.pinRoleOf.get('open-a'), 'opener');
	assert.equal(queue.autoPlayOrder.pinRoleOf.get('peak-b'), 'peak');
	assert.equal(queue.autoPlayOrder.pinRoleOf.get('close-c'), 'closer');
});

test('publishAutoPlayOrder does not assign pinRoleOf', () => {
	const start = QUEUE_SOURCE.indexOf('export function publishAutoPlayOrder');
	const end = QUEUE_SOURCE.indexOf('export function publishSetGoalPins', start);
	const body = QUEUE_SOURCE.slice(start, end);
	assert.doesNotMatch(body, /pinRoleOf/);
});

test('clearAutoPlayOrder clears pin roles', () => {
	queue.publishSetGoalPins(new Map([['peak-b', 'peak']]));
	queue.clearAutoPlayOrder();
	assert.equal(queue.autoPlayOrder.pinRoleOf.size, 0);
});

test('AutoPlay rank glyph stays numeric arrow only in extracted cell', () => {
	const source = readFileSync(
		fileURLToPath(
			new URL('../../src/lib/components/rb/browser/AutoPlayRankCell.svelte', import.meta.url)
		),
		'utf8'
	);
	assert.match(source, />\{rank\}\{AUTOPLAY_ARROW\}<\/span>/);
	assert.match(source, /class="ap-pin-role"/);
});
