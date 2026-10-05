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

// Review thread on takeover-engine-sync.svelte.ts:67 (PR #3837): the glue
// acknowledges a hardware value BEFORE its command is dispatched, so a
// rejected command (a preset lifecycle lock) leaves the engine scalar where it
// was. The policy must not treat that acknowledged-but-unapplied value as
// picked up: the next observation carries the unchanged engine value, and that
// mismatch is what re-arms pickup.
test('IOPIN-06: an acknowledged value the engine never took is re-armed by the next observation', () => {
	const policy = new AbsoluteTakeoverPolicy();
	const engineValue = 0.5;
	// Picked up at the engine value and applied normally.
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.5, softwareValue: engineValue }).apply, true);
	policy.noteHardwareApplied(identity, 0.5);
	// The fader moves; the glue acknowledges 0.7, then the dispatch is rejected.
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.7, softwareValue: engineValue }).apply, true);
	policy.noteHardwareApplied(identity, 0.7);
	// The engine still reads 0.5. Moving further away must be HELD, with the
	// ghost naming the engine value, never applied as a jump once the lock lifts.
	const held = policy.observeAbsolute({ identity, hardwareValue: 0.9, softwareValue: engineValue });
	assert.deepEqual(held, { apply: false, ghost: { value: 0.9, target: 0.5 } });
	assert.deepEqual(policy.ghostForFunction('mixer:1:trim'), { value: 0.9, target: 0.5 });
	// It picks up again only by coming back across the engine value.
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.45, softwareValue: engineValue }).apply, true);
});

// The opposite direction: when the dispatch DID land, the engine reads the
// acknowledged value and the control must keep tracking without a re-pickup.
test('IOPIN-06: an acknowledged value the engine took keeps tracking', () => {
	const policy = new AbsoluteTakeoverPolicy();
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.5, softwareValue: 0.5 }).apply, true);
	policy.noteHardwareApplied(identity, 0.5);
	assert.equal(policy.observeAbsolute({ identity, hardwareValue: 0.7, softwareValue: 0.5 }).apply, true);
	policy.noteHardwareApplied(identity, 0.7);
	const next = policy.observeAbsolute({ identity, hardwareValue: 0.9, softwareValue: 0.7 });
	assert.deepEqual(next, { apply: true, ghost: null });
});
