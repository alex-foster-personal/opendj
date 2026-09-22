// IOPIN-06: absolute MIDI must not jump a software-edited scalar until the
// physical control catches up or crosses it. Relative encoders stay outside
// this policy and are deliberately tested at the action-glue boundary.

import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	AbsoluteTakeoverPolicy,
	continuousTakeoverFunction,
	makeTakeoverIdentity
} from '../../src/lib/rb/midi/takeover-policy.ts';

const trim = { type: 'mixer_channel', deck: 1, target: 'trim' };
const identity = makeTakeoverIdentity('mixtour-a', 'cc:1:11', trim);

test('IOPIN-06: pickup holds an absolute control until it reaches or crosses the software target', () => {
	const policy = new AbsoluteTakeoverPolicy();
	assert.equal(continuousTakeoverFunction(trim), 'mixer:1:trim');

	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.1, softwareValue: 0.7 }).apply, false);
	assert.deepEqual(policy.ghostForFunction('mixer:1:trim'), { value: 0.1, target: 0.7 });
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.5, softwareValue: 0.7 }).apply, false);
	// Crosses 0.7 between the previous physical position and this message.
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.8, softwareValue: 0.7 }).apply, true);
	assert.equal(policy.ghostForFunction('mixer:1:trim'), null);
});

test('IOPIN-06: one MIDI step is pickup, software edits rearm and retain the observed ghost', () => {
	const policy = new AbsoluteTakeoverPolicy();
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.5, softwareValue: 0.5 }).apply, true);
	policy.noteSoftwareValue('mixer:1:trim', 0.8);
	assert.deepEqual(policy.ghostForFunction('mixer:1:trim'), { value: 0.5, target: 0.8 });
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.8 - 1 / 127, softwareValue: 0.8 }).apply, true);
});

test('IOPIN-06: Jump is explicit override; reconnect and layer switches rearm without inventing position', () => {
	const policy = new AbsoluteTakeoverPolicy('jump');
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.1, softwareValue: 0.8 }).apply, true);
	policy.setMode('pickup');
	policy.rearmDevice('mixtour-a');
	// The previous observation remains an honest ghost, but the next message
	// is held again until physical and software positions meet.
	assert.deepEqual(policy.ghostForFunction('mixer:1:trim'), { value: 0.1, target: 0.1 });
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.2, softwareValue: 0.8 }).apply, false);
});

test('IOPIN-06: returning to a bank rearms its old physical control state', () => {
	// [if] a bank returns [then] its prior hardware position must pick up, [else stop].
	const policy = new AbsoluteTakeoverPolicy();
	// controlId is the binding key: the shift/layer bit deliberately keeps each
	// binding separate even when the physical CC number is the same.
	const bankA = makeTakeoverIdentity('mixtour-a', '0|1|cc|11', trim);
	const bankB = makeTakeoverIdentity('mixtour-a', '1|1|cc|11', {
		type: 'mixer_channel', deck: 2, target: 'trim'
	});
	assert.equal(policy.observeAbsolute({ identity: bankA, hardwareValue: 0.2, softwareValue: 0.2 }).apply, true);
	policy.rearmDevice('mixtour-a'); // bank A -> B
	assert.equal(policy.observeAbsolute({ identity: bankB, hardwareValue: 0.8, softwareValue: 0.8 }).apply, true);
	policy.rearmDevice('mixtour-a'); // bank B -> A
	const returned = policy.observeAbsolute({ identity: bankA, hardwareValue: 0.8, softwareValue: 0.2 });
	assert.equal(returned.apply, false, 'bank A must not jump from bank B\'s last physical value');
	assert.deepEqual(returned.ghost, { value: 0.8, target: 0.2 });
});
