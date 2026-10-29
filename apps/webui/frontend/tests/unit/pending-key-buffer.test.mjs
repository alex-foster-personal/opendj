/**
 * Compatible dropdown keys pressed while its lazy panel is still loading
 * (PR #6574, review 4393984663).
 *
 * [if] a T / M pressed before the panel binds is dropped [then] the first shortcut on a cold load does nothing - broken.
 * [if] held keys replay out of order or twice [then] T then M lands on the wrong mode - broken.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
before(async () => {
	m = await loadTypeScriptModule('src/lib/components/rb/browser/pending-key-buffer.ts');
});

function key(k, mods = {}) {
	return { key: k, metaKey: false, ctrlKey: false, altKey: false, ...mods, prevented: 0, preventDefault() { this.prevented += 1; } };
}

test('pending import: T then M are held and replay in order, once, when the panel binds', () => {
	const buffer = new m.PendingKeyBuffer();
	const t = key('t');
	const mm = key('M');
	assert.equal(buffer.hold(t), true);
	assert.equal(buffer.hold(mm), true);
	assert.equal(t.prevented, 1, 'held keys stop their default at press time');
	const seen = [];
	buffer.flush((e) => seen.push(e.key));
	assert.deepEqual(seen, ['t', 'M']);
	buffer.flush((e) => seen.push(e.key));
	assert.deepEqual(seen, ['t', 'M'], 'a second flush replays nothing');
});

test('keys the panel ignores, and modified chords, are never held', () => {
	const buffer = new m.PendingKeyBuffer();
	assert.equal(buffer.hold(key('x')), false);
	assert.equal(buffer.hold(key('Tab')), false);
	assert.equal(buffer.hold(key('m', { metaKey: true })), false);
	assert.equal(buffer.size, 0);
	// Control: an arrow key is held.
	assert.equal(buffer.hold(key('ArrowDown')), true);
});

test('closing before the panel arrives drops held keys', () => {
	const buffer = new m.PendingKeyBuffer();
	buffer.hold(key('m'));
	buffer.clear();
	const seen = [];
	buffer.flush((e) => seen.push(e.key));
	assert.deepEqual(seen, []);
});

test('the popover holds keys while panelRef is unbound and flushes when it binds', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/browser/CompatibleFilterPopover.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /if \(panelRef === undefined\) \{\n\t\t\tpendingKeys\.hold\(e\);/);
	assert.match(src, /pendingKeys\.flush\(\(e\) => panel\.handleKey\(e\)\)/);
	assert.doesNotMatch(src, /if \(!open \|\| panelRef === undefined\) return;/);
});
