/**
 * P1 (Codex, #5822): a folded sync_to_out launch must tell the DJ through the
 * shared Beat Sync notice path, the orange warn toast and the beat-sync-fold
 * perf row the BAR contract requires. The plan's beat_sync prepare command
 * carries the fold; the executor raises the notice when it runs, because a
 * paused incoming deck never reaches the engine's own notice path.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let bsm;

before(async () => {
	bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

test('a folded launch to half tempo raises the orange fold notice naming the deck', () => {
	const [notice] = bsm.foldedLaunchNotices(2, 1, 0.5);
	assert.equal(notice.kind, 'warn');
	assert.equal(notice.message, 'BAR sync locked with a tempo fold (deck 2 at half tempo) - phase holds, the bar count does not');
	assert.equal(notice.groupKey, 'beat-sync-fold:1');
	assert.deepEqual(notice.events, [{ kind: 'beat-sync-fold', detail: 'tempoNormalization=0.5', deck: 2 }]);
});

test('a folded launch to double tempo says double', () => {
	const [notice] = bsm.foldedLaunchNotices(3, 1, 2);
	assert.match(notice.message, /\(deck 3 at double tempo\)/);
});

test('control: an unfolded launch raises no fold notice', () => {
	assert.deepEqual(bsm.foldedLaunchNotices(2, 1, 1), []);
});

test('SOURCE: the executor takes the fold off beat_sync and raises the notice on the shared path', () => {
	const ipc = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.match(ipc, /_exactKeys\(record, \['type', 'deck', 'enabled', 'normalization', 'master'\]\);/);
	assert.match(ipc, /normalization: _beatSyncNormalization\(record\.normalization\)/);
	assert.match(ipc, /\/\/ Before either engine[^\n]*\n\tif \(command\.type === 'beat_sync' && command\.enabled && command\.normalization !== undefined && command\.master !== undefined\) \{\n\t\treportFoldedLaunch\(command\.deck, command\.master, command\.normalization\);\n\t\}\n\tif \(await executeInRustEngine\(command, pushToast\)\)/);
	assert.match(ipc, /import \{ deckFacingMessage, reportFoldedLaunch \} from '\$lib\/rb\/deck-load-context';/);
	const dlc = readFrontendSource('src/lib/rb/deck-load-context.ts');
	assert.match(dlc, /for \(const notice of foldedLaunchNotices\(deck, master, normalization\)\) \{\n\t\tpushToast\(notice\.message, notice\.kind, undefined, undefined, \{\}, notice\.groupKey\);/);
});

test('SOURCE: the command type names the optional fold and its master', () => {
	const ipc = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.match(ipc, /\| \{ type: 'beat_sync'; deck: DeckId; enabled: boolean; normalization\?: 0\.5 \| 2; master\?: DeckId \}/);
});
