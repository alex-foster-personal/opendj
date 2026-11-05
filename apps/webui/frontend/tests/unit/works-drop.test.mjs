import { test } from 'node:test';
import assert from 'node:assert/strict';
import { shiftDropAction } from '../../src/lib/components/rb/browser/works-drop.ts';

const workOf = (id) => ({ a: 'w', b: 'w', c: 'x' })[id] ?? null;

test('a shift-drop onto itself does nothing', () => {
	assert.equal(shiftDropAction(['a'], 'a', workOf), 'nothing');
});
test('a shift-drop within one work says already versions and creates nothing', () => {
	assert.equal(shiftDropAction(['a'], 'b', workOf), 'already');
});
test('a shift-drop of a plain track links it', () => {
	assert.equal(shiftDropAction(['z'], 'a', workOf), 'link');
	assert.equal(shiftDropAction(['z'], 'y', workOf), 'link');
});
test('two different works still go to the server, which refuses and asks', () => {
	assert.equal(shiftDropAction(['a'], 'c', workOf), 'link');
});
