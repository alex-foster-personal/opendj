import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * CTRL-10093 - export an installed MIDI map to a JSON file and import one,
 * with a diff before replacing (rekordbox MIDI EXPORT / IMPORT, mimicked).
 *
 * Regression lines:
 * - if the exported text does not parse back equal to the GET document then broken
 * - if schemaVersion is not the first key of the exported file then broken
 * - if parseMapFile accepts a file without schemaVersion 1, id, nameMatch or bindings then broken
 * - if diffMapDocs reports a change for identical docs then broken
 * - if diffMapDocs misses a changed, added or removed binding then broken
 */

let mt;

before(async () => {
	mt = await loadTypeScriptModule('src/lib/components/rb/midi/midi-map-transfer.ts');
});

const prov = { tier: 'learned', cite: 'learn session', verified: false };

/** Build one binding fixture. */
function binding(id, deck = 1, over = {}) {
	return {
		source: { ch: 1, kind: 'note', id },
		action: { type: 'deck_play_toggle', deck },
		provenance: prov,
		...over
	};
}

/** Build the GET document fixture, with schemaVersion deliberately not first. */
function fixture() {
	return {
		id: 'acme-dj1',
		vendor: 'Acme',
		model: 'DJ-1',
		nameMatch: '^Acme DJ-1',
		schemaVersion: 1,
		bindings: [binding(11), binding(12, 2)],
		leds: [],
		meters: [],
		init: []
	};
}

test('exportFileName names the file <id>.opendj-midi-map.json', () => {
	assert.equal(mt.exportFileName('acme-dj1'), 'acme-dj1.opendj-midi-map.json');
});

test('serialiseMapDoc puts schemaVersion first and parses back equal to the fixture', () => {
	const text = mt.serialiseMapDoc(fixture());
	const parsed = JSON.parse(text);
	assert.deepEqual(parsed, fixture());
	assert.equal(Object.keys(parsed)[0], 'schemaVersion');
	const round = mt.parseMapFile(text);
	assert.equal(round.ok, true);
	assert.deepEqual(round.doc, fixture());
});

test('parseMapFile rejects a file missing a required field with a named reason', () => {
	const cases = [
		['schemaVersion', 'schemaVersion must be 1'],
		['id', 'missing id'],
		['nameMatch', 'missing nameMatch'],
		['bindings', 'missing bindings']
	];
	for (const [field, reason] of cases) {
		const doc = fixture();
		delete doc[field];
		assert.deepEqual(mt.parseMapFile(JSON.stringify(doc)), { ok: false, reason }, field);
	}
	assert.deepEqual(mt.parseMapFile(JSON.stringify({ ...fixture(), schemaVersion: 2 })), {
		ok: false,
		reason: 'schemaVersion must be 1'
	});
	assert.equal(mt.parseMapFile('{not json').ok, false);
	assert.deepEqual(mt.parseMapFile('[]'), { ok: false, reason: 'not a JSON object' });
	assert.deepEqual(mt.parseMapFile(JSON.stringify({ ...fixture(), bindings: [{}] })), {
		ok: false,
		reason: 'binding 0 has no source or action'
	});
});

test('diffMapDocs on identical docs is empty', () => {
	const diff = mt.diffMapDocs(fixture(), fixture());
	assert.deepEqual(diff, { added: [], removed: [], changed: [] });
	assert.equal(mt.isEmptyDiff(diff), true);
});

test('diffMapDocs reports one changed binding by source key', () => {
	const next = fixture();
	next.bindings[1] = binding(12, 3);
	const diff = mt.diffMapDocs(fixture(), next);
	assert.deepEqual(diff.added, []);
	assert.deepEqual(diff.removed, []);
	assert.equal(diff.changed.length, 1);
	assert.equal(diff.changed[0].key, 'note:1:12');
	assert.equal(diff.changed[0].before.action.deck, 2);
	assert.equal(diff.changed[0].after.action.deck, 3);
});

test('diffMapDocs reports one added and one removed binding', () => {
	const next = fixture();
	next.bindings = [binding(11), binding(12, 2, { shift: true })];
	const diff = mt.diffMapDocs(fixture(), next);
	assert.deepEqual(diff.changed, []);
	assert.deepEqual(diff.added.map(mt.bindingSourceKey), ['note:1:12:shift']);
	assert.deepEqual(diff.removed.map(mt.bindingSourceKey), ['note:1:12']);
	assert.equal(mt.isEmptyDiff(diff), false);
});

test('renameImport changes only id and nameMatch for a clash retry', () => {
	const renamed = mt.renameImport(fixture(), 'acme-dj1-b', '^Acme DJ-1 B');
	assert.deepEqual(renamed, { ...fixture(), id: 'acme-dj1-b', nameMatch: '^Acme DJ-1 B' });
});
