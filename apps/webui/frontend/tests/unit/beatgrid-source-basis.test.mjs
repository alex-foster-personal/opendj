/**
 * beatgrid-source-basis.ts: which /anlz payloads may differ from the
 * lane-wide rbx-vs-own selection (STANDALONE-06, issue #3536).
 *
 * Regression lines:
 * - if an unmapped track's unmapped-default own payload disagrees with a rekordbox selection then deck load refetches /anlz forever - broken
 * - if an own payload with basis 'selection' passes a rekordbox selection then a straggler from a source switch is published - broken
 * - if a payload with no basis passes a mismatched selection then an older engine's stragglers are published - broken
 * - if an unmapped-default payload passes an explicit own selection check in the wrong direction (rekordbox served, own selected) then a stale rbx grid is kept - broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let basis;
before(async () => {
	basis = await loadTypeScriptModule('src/lib/rb/beatgrid-source-basis.ts');
});

const payload = (beatgrid_source, beatgrid_source_basis) => ({ beatgrid_source, beatgrid_source_basis });

test('if an unmapped-default own payload disagrees with a rekordbox selection then deck load refetches /anlz forever - broken', () => {
	assert.equal(basis.anlzSourceMatchesSelection(payload('own', 'unmapped-default'), 'rekordbox'), true);
	assert.equal(basis.isUnmappedDefault(payload('own', 'unmapped-default')), true);
});

test('if an own payload with basis selection passes a rekordbox selection then a switch straggler is published - broken', () => {
	assert.equal(basis.anlzSourceMatchesSelection(payload('own', 'selection'), 'rekordbox'), false);
	assert.equal(basis.isUnmappedDefault(payload('own', 'selection')), false);
});

test('if a payload with no basis passes a mismatched selection then an older engine straggler is published - broken', () => {
	assert.equal(basis.anlzSourceMatchesSelection(payload('own', undefined), 'rekordbox'), false);
	assert.equal(basis.anlzSourceMatchesSelection(payload('rekordbox', undefined), 'own'), false);
	assert.equal(basis.anlzSourceMatchesSelection(payload('own', undefined), 'own'), true);
});

test('if a rekordbox payload passes an own selection then a stale rbx grid is kept - broken', () => {
	assert.equal(basis.anlzSourceMatchesSelection(payload('rekordbox', 'selection'), 'own'), false);
	assert.equal(basis.anlzSourceMatchesSelection(payload('rekordbox', 'unmapped-default'), 'own'), false);
	assert.equal(basis.isUnmappedDefault(payload('rekordbox', 'unmapped-default')), false);
});
