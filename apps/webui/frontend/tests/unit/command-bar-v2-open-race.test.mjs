/**
 * CMDK open race (Codex P2 BLOCKING, #6692): two cmdk_open calls that do not await each other must
 * leave the later query in place, even when the first call resumes after the second has written.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');

test('CMDK-open-race: a stale open never overwrites a newer open query', () => {
	assert.match(source, /let openTurn = 0;/);
	assert.match(source, /const turn = \+\+openTurn;/);
	// The stale check follows the awaited open and guards the query write.
	const adapter = source.slice(source.indexOf('const turn = ++openTurn;'), source.indexOf('close: () => closeBar()'));
	assert.ok(adapter.indexOf('if (turn !== openTurn) return;') > adapter.indexOf('await openBar()'));
	assert.ok(adapter.indexOf('if (turn !== openTurn) return;') < adapter.indexOf('query = text'));
});
