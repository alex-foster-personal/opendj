/** Skin "try for 30 s" trial state machine contract. */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const trial = await loadTypeScriptModule('src/lib/rb/skin-trial.ts');

test('after 29.9 s the trial skin is active with 1 second left', () => {
	const s = trial.tick(trial.startTrial('classic', 'neon', 1000), 1000 + 29_900);
	assert.equal(s.active, 'neon');
	assert.equal(s.secondsLeft, 1);
	assert.equal(s.status, 'trying');
});

test('30 s without keep reverts to the previous skin', () => {
	const s = trial.tick(trial.startTrial('classic', 'neon', 0), 30_000);
	assert.equal(s.status, 'reverted');
	assert.equal(s.active, 'classic');
});

test('keep before 30 s holds the trial skin through later ticks', () => {
	let s = trial.tick(trial.startTrial('classic', 'neon', 0), 10_000);
	s = trial.keep(s);
	s = trial.tick(s, 60_000);
	assert.equal(s.status, 'kept');
	assert.equal(s.active, 'neon');
});

test('starting a trial with the current skin throws naming the skin id', () => {
	assert.throws(() => trial.startTrial('neon', 'neon', 0), /neon/);
});

test('cancel reverts immediately', () => {
	const s = trial.cancel(trial.startTrial('classic', 'neon', 0));
	assert.equal(s.active, 'classic');
});
