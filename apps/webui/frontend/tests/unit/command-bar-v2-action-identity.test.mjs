/**
 * CMDK action identity (Codex P2 BLOCKING, #6691): a setting's wire id carries the value it shows, so a pick
 * read before the setting moved no longer matches the row, and the pick is refused rather than applied to
 * the opposite effect. The dialog snapshot also reports a replace confirmation as `confirming`.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pairs;

before(async () => {
	pairs = await loadTypeScriptModule('src/lib/rb/cmdk-pairs.ts');
});

test('CMDK-action-identity: a setting id changes with its value, a command id does not', () => {
	const on = pairs.cmdkWireId({ kind: 'action', action: { id: 'setting.wave', value: true } });
	const off = pairs.cmdkWireId({ kind: 'action', action: { id: 'setting.wave', value: false } });
	assert.notEqual(on, off, 'a value change must change the row id, so a stale pick is refused');
	assert.equal(on, pairs.cmdkWireId({ kind: 'action', action: { id: 'setting.wave', value: true } }));
	assert.equal(pairs.cmdkWireId({ kind: 'action', action: { id: 'command.open' } }), 'command.open');
});

test('CMDK-action-identity: the deck picker reports a replace confirmation as confirming', () => {
	const source = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	assert.match(source, /kind: 'deck-picker' as const, deck: picker\.sel, title: picker\.row\.title \?\? '\(untitled\)', confirming: picker\.guard !== null/);
});

test('CMDK-action-identity: a setting row reports the id cmdk_pick validates, so a read id is accepted', () => {
	const source = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	assert.match(source, /kind: 'setting' as const,\n\s*\/\/[^\n]*\n\s*id: cmdkWireId\(entry\),/);
	assert.doesNotMatch(source, /kind: 'setting' as const,\n\s*id: entry\.action\.id,/);
});

test('CMDK-action-identity: an Open settings row is identified by its query, so a moved query refuses a stale pick', () => {
	const at = (query) => pairs.cmdkWireId({ kind: 'action', action: { id: 'open-settings', command: { type: 'open_settings', query } } });
	assert.notEqual(at('a'), at('ab'), 'a changed query must change the row id');
	assert.equal(at('a'), at('a'));
	assert.equal(pairs.cmdkWireId({ kind: 'action', action: { id: 'eject-1:3', command: { type: 'eject', deck: 1 } } }), 'eject-1:3');
});

test('CMDK-action-identity: an enum setting id carries the value Enter writes, so a query that moves the target refuses a stale pick', () => {
	const at = (next) => pairs.cmdkWireId({ kind: 'action', action: { id: 'setting.theme', value: 'dark', next } });
	assert.notEqual(at('light'), at('system'), 'a moved enum target must change the row id');
	assert.equal(at('light'), at('light'));
});

test('CMDK-action-identity: the command row publishes the id cmdk_pick validates, and settled guards are gated to their bar session', () => {
	const source = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	assert.match(source, /\{ kind: 'command' as const, id: cmdkWireId\(entry\), label: entry\.action\.label \}/);
	assert.doesNotMatch(source, /kind: 'command' as const, id: entry\.action\.id,/);
	assert.match(source, /async function settledGuard\(id: DeckId, session: number\)/);
	assert.match(source, /if \(barSession === session\) actionError = `Deck \$\{id\} is still busy/);
	assert.match(source, /if \(guard !== null && picker === p && barSession === session\) p\.guard = guard;/);
	assert.match(source, /if \(op !== ejectOp \|\| ejectGuard !== null \|\| barSession !== session\) return;/);
});
