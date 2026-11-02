// requirement: DECKGAP-7691c
// Ported from Preview (3ec8ad5495, 4f33b5d5b3): a deck that plays without its
// phase-lock says so on its own Play button, a red glow with a red hover warning.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8');

let math;
before(async () => {
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

test('a tempo-range lock failure reads as the plain-language warning, detail kept', () => {
	// if the DJ sees "no phase-capable bar anchor with tempo ratio within [0.84, 1.16]", the cause is unreadable -- broken.
	const detail = 'follower grid has no phase-capable bar anchor with tempo ratio within [0.84, 1.16] for beat n=2';
	const error = new math.TempoOutOfSyncRangeError(detail);
	assert.ok(error instanceof RangeError, 'still a RangeError for callers that only know that');
	assert.equal(math.TEMPO_TOO_FAR_TO_SYNC, 'tempo is too far from master to beat sync');
	assert.equal(math.describeUnsyncedStart(error), `${math.TEMPO_TOO_FAR_TO_SYNC} (${detail})`);
	assert.equal(math.unsyncedPlayWarning(math.describeUnsyncedStart(error)), math.TEMPO_TOO_FAR_TO_SYNC);
});

test('any other lock failure keeps its own words', () => {
	// Control: the tempo wording must not be pasted over an unrelated cause.
	assert.equal(
		math.unsyncedPlayWarning('phase lock lost: worklet died'),
		'Beat Sync is not locked: phase lock lost: worklet died'
	);
});

test('the planner names a tempo out of range with the typed error', () => {
	const source = read('lib/rb/beat-sync-math.ts');
	assert.match(source, /throw new TempoOutOfSyncRangeError\(\n\t\t`follower grid has no phase-capable/);
});

test('the Play button glows and explains while the deck is not locked', () => {
	const cluster = read('lib/components/rb/deck/TransportCluster.svelte');
	assert.match(cluster, /unsyncedPlayWarning\(deck\.sync_error\)/);
	assert.match(cluster, /class:sync-warning=\{syncWarning !== null\}/);
	assert.match(cluster, /data-sync-warning=\{syncWarning \?\? undefined\}/);
	assert.match(cluster, /role="tooltip"[^>]*>\{syncWarning\}/);
	assert.match(cluster, /\.round\.play\.sync-warning \{[^}]*box-shadow:[^}]*var\(--rb-danger/);
	// Control: no track loaded means no warning, whatever sync_error holds.
	assert.match(cluster, /hasTrack && deck\.sync_error !== null \? unsyncedPlayWarning/);
});
