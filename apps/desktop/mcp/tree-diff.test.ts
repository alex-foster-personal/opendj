// Run: pnpm --dir apps/desktop exec tsx --test mcp/tree-diff.test.ts
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { diffTrees, refless } from './tree-diff';

const CAP = 80;

test('losing one of two identical nodes is a change', () => {
	const before = ['list', '  button "Remove" @e1', '  button "Remove" @e2'];
	const after = ['list', '  button "Remove" @e1'];
	assert.equal(diffTrees(before, after, CAP), '- button "Remove" @e2');
});

test('gaining a duplicate node is a change', () => {
	const before = ['list', '  row "Track" @e1'];
	const after = ['list', '  row "Track" @e1', '  row "Track" @e2'];
	assert.equal(diffTrees(before, after, CAP), '+ row "Track" @e2');
});

test('refs renumbered by an insertion above are not changes', () => {
	const before = ['main', '  button "Play" @e1', '  button "Cue" @e2'];
	const after = ['main', '  dialog "Settings" @e1', '  button "Play" @e2', '  button "Cue" @e3'];
	assert.equal(diffTrees(before, after, CAP), '+ dialog "Settings" @e1');
});

test('an unchanged tree reports no change', () => {
	const tree = ['list', '  button "Remove" @e1', '  button "Remove" @e2'];
	assert.equal(diffTrees(tree, [...tree], CAP), '(no tree change)');
});

test('a large change names the new dialog first', () => {
	const before = Array.from({ length: 100 }, (_, i) => `  text "old ${i}" @e${i}`);
	const after = ['    text "shallow?"', '  dialog "Settings" @e1', ...Array.from({ length: 100 }, (_, i) => `  text "new ${i}"`)];
	const out = diffTrees(before, after, CAP);
	assert.match(out, /^\(large change/);
	assert.equal(out.split('\n')[1], '+ dialog "Settings" @e1');
});

test('a small change names the new dialog before a removed row', () => {
	const before = ['list', '  row "Track A" @e1', '  row "Track B" @e2'];
	const after = ['list', '  row "Track B" @e1', '  text "1 track" @e2', '  dialog "Undo?" @e3'];
	assert.equal(diffTrees(before, after, CAP), '+ dialog "Undo?" @e3\n- row "Track A" @e1\n+ text "1 track" @e2');
});

test('only the trailing ref is stripped, not ref-like text inside a name', () => {
	assert.equal(refless('  button "Send to @e12" @e3'), '  button "Send to @e12"');
	assert.notEqual(refless('  button "Send to @e12" @e3'), refless('  button "Send to" @e4'));
});
