import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Opt-in startup master mute -- the silence belt for headless multi-browser UI
 * test agents, behind `?muted=1`.
 *
 * Regression lines:
 * - if `?muted=1` does not drive the master mute gain to exactly 0 then a
 *   fan-out of headless agents plays audio out loud
 * - if a value other than '1' mutes then a stray query param silences the
 *   headed client the maintainer is actually listening to
 * - if unmuting does not restore exactly 1 then the mute is lossy and the app
 *   comes back quieter than it started
 * - if muting disconnects, bypasses or re-wires any node then a silent browser
 *   stops exercising the audio path and audio bugs hide until a headed run
 * - if setMasterMuted accepts a non-boolean then '0' (truthy) silences the app
 */

let mute;

/** Minimal stand-in for the engine's final GainNode. Records every graph
 * mutation so a test can prove muting touched the VALUE and nothing else. */
function fakeGainNode() {
	const node = {
		gain: { value: 1 },
		connections: [],
		disconnectCalls: 0,
		connect(target) {
			node.connections.push(target);
			return target;
		},
		disconnect() {
			node.disconnectCalls += 1;
			node.connections.length = 0;
		}
	};
	return node;
}

before(async () => {
	mute = await loadTypeScriptModule('src/lib/player/master-mute.svelte.ts');
});

//-----------------------------------------------------------------------------
// query parameter
//-----------------------------------------------------------------------------

test('only the exact value 1 arms the startup mute', () => {
	assert.equal(mute.MASTER_MUTE_PARAM, 'muted');
	assert.equal(mute.parseMasterMutedParam('?muted=1'), true);
	assert.equal(mute.parseMasterMutedParam('muted=1'), true);
	assert.equal(mute.parseMasterMutedParam('?deck=2&muted=1&extroute=1:1'), true);
});

test('an absent parameter leaves the page audible', () => {
	assert.equal(mute.parseMasterMutedParam(''), false);
	assert.equal(mute.parseMasterMutedParam('?'), false);
	assert.equal(mute.parseMasterMutedParam('?deck=2'), false);
});

test('a garbage or near-miss value is ignored rather than guessed at', () => {
	// Every one of these would silence the maintainer's headed client if the parser were
	// permissive, so the strict '1' match is the whole point.
	for (const raw of ['0', 'true', 'TRUE', 'yes', 'on', '', '2', '1.0', ' 1', '1x', 'null']) {
		assert.equal(
			mute.parseMasterMutedParam(`?muted=${encodeURIComponent(raw)}`),
			false,
			`if muted=${JSON.stringify(raw)} mutes then a stray param silences the headed client`
		);
	}
});

test('a repeated parameter reads the first occurrence, not a merged value', () => {
	assert.equal(mute.parseMasterMutedParam('?muted=1&muted=0'), true);
	assert.equal(mute.parseMasterMutedParam('?muted=0&muted=1'), false);
});

test('startup is unmuted with no window, so SSR never mutes anything', () => {
	assert.equal(typeof globalThis.window, 'undefined');
	assert.equal(mute.startupMasterMuted(), false);
});

// [if] the unit runner installs a fake window without location [then] startupMasterMuted
// does not throw and the page stays audible
test('startup is unmuted when window exists but location.search is missing', async () => {
	globalThis.window = {
		localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} }
	};
	try {
		const fresh = await loadTypeScriptModule('src/lib/player/master-mute.svelte.ts');
		assert.equal(fresh.startupMasterMuted(), false);
		assert.equal(fresh.isMasterMuted(), false);
	} finally {
		delete globalThis.window;
	}
});

//-----------------------------------------------------------------------------
// gain endpoints
//-----------------------------------------------------------------------------

test('mute endpoints are exactly 0 and exactly 1, never a ramp or an epsilon', () => {
	assert.equal(mute.masterMuteGainValue(true), 0);
	assert.equal(mute.masterMuteGainValue(false), 1);
	assert.equal(mute.MASTER_MUTE_GAIN, 0);
	assert.equal(mute.MASTER_UNMUTE_GAIN, 1);
});

//-----------------------------------------------------------------------------
// setter and node
//-----------------------------------------------------------------------------

test('the engine default is audible until something asks for silence', () => {
	assert.equal(mute.isMasterMuted(), false);
	assert.equal(mute.masterMuteNode(), null);
});

test('muting sets the master gain to exactly 0 and unmuting restores exactly 1', () => {
	const node = fakeGainNode();
	mute.attachMasterMuteNode(node);
	assert.equal(node.gain.value, 1);

	mute.setMasterMuted(true);
	assert.equal(node.gain.value, 0);
	assert.equal(mute.isMasterMuted(), true);

	mute.setMasterMuted(false);
	assert.equal(node.gain.value, 1);
	assert.equal(mute.isMasterMuted(), false);

	mute.attachMasterMuteNode(null);
});

test('setter round-trips through the getter across repeats and both directions', () => {
	const node = fakeGainNode();
	mute.attachMasterMuteNode(node);

	for (const wanted of [true, true, false, false, true, false]) {
		mute.setMasterMuted(wanted);
		assert.equal(mute.isMasterMuted(), wanted);
		assert.equal(node.gain.value, wanted ? 0 : 1);
	}

	mute.attachMasterMuteNode(null);
});

test('a non-boolean is refused loudly instead of being coerced', () => {
	const node = fakeGainNode();
	mute.attachMasterMuteNode(node);

	// '0' and 0 are the dangerous pair: one is truthy, one is falsy.
	for (const bad of ['0', '1', 0, 1, null, undefined, {}]) {
		assert.throws(() => mute.setMasterMuted(bad), TypeError);
	}
	assert.equal(mute.isMasterMuted(), false);
	assert.equal(node.gain.value, 1);

	mute.attachMasterMuteNode(null);
});

//-----------------------------------------------------------------------------
// the graph must survive the mute
//-----------------------------------------------------------------------------

test('muting never disconnects the node, so the graph stays wired while silent', () => {
	const node = fakeGainNode();
	const destination = { name: 'destination' };
	mute.attachMasterMuteNode(node);
	node.connect(destination);

	mute.setMasterMuted(true);
	assert.equal(node.disconnectCalls, 0);
	assert.deepEqual(node.connections, [destination]);
	assert.equal(node.gain.value, 0);

	mute.setMasterMuted(false);
	assert.equal(node.disconnectCalls, 0);
	assert.deepEqual(node.connections, [destination]);

	mute.attachMasterMuteNode(null);
});

test('muting touches the gain value and nothing else on the node', () => {
	const node = fakeGainNode();
	mute.attachMasterMuteNode(node);
	const before = { ...node, gain: { ...node.gain }, connections: [...node.connections] };

	mute.setMasterMuted(true);

	assert.notEqual(node.gain.value, before.gain.value);
	assert.equal(node.disconnectCalls, before.disconnectCalls);
	assert.deepEqual(node.connections, before.connections);
	assert.equal(node.channelCount, before.channelCount);
	assert.equal(node.channelInterpretation, before.channelInterpretation);

	mute.setMasterMuted(false);
	mute.attachMasterMuteNode(null);
});

test('attaching a node stamps the standing mute onto it, so a rebuilt graph stays silent', () => {
	mute.setMasterMuted(true);

	// Route remount: the old node is dropped and a fresh one adopted. A headless
	// agent must not get its audio back just because the graph was rebuilt.
	mute.attachMasterMuteNode(null);
	assert.equal(mute.isMasterMuted(), true);

	const rebuilt = fakeGainNode();
	assert.equal(rebuilt.gain.value, 1);
	mute.attachMasterMuteNode(rebuilt);
	assert.equal(rebuilt.gain.value, 0);
	assert.equal(mute.masterMuteNode(), rebuilt);

	mute.setMasterMuted(false);
	assert.equal(rebuilt.gain.value, 1);
	mute.attachMasterMuteNode(null);
});

test('the setter works before any node exists and applies on attach', () => {
	assert.equal(mute.masterMuteNode(), null);
	mute.setMasterMuted(true);
	assert.equal(mute.isMasterMuted(), true);

	const node = fakeGainNode();
	mute.attachMasterMuteNode(node);
	assert.equal(node.gain.value, 0);

	mute.setMasterMuted(false);
	mute.attachMasterMuteNode(null);
});

//-----------------------------------------------------------------------------
// engine wiring
//
// The mute is only a mute if it is the LAST node before the destination. That
// is a property of how _ensureGraph strings the nodes together, and the engine
// never returns the wiring to a caller, so it is pinned as source text -- the
// same drift-guard shape control-explainer-phase-lock and load-memory-kpis use.
//-----------------------------------------------------------------------------

test('the mute gain is the final node before the destination on both output paths', () => {
	const body = engineBlockAfter('function _ensureGraph(): AudioContext {');

	// Internal path: master bus -> mute -> speakers.
	assert.ok(
		body.includes('_masterMuteGain.connect(_ctx.destination)'),
		'if the mute gain does not feed _ctx.destination then muting silences nothing'
	);
	assert.ok(
		body.includes('wirePracticeBlendIntoMasterPath(_masterGain, _masterMuteGain, headphones)'),
		'if the master bus does not feed the mute gain then the mute is out of the chain'
	);
	assert.ok(
		body.includes('wireSplitCableIntoMasterPath(_masterGain, _masterMuteGain, headphones)'),
		'if the split-cable path does not feed the mute gain then split_cable is out of the chain'
	);

	// External-mixer path (?extroute=): merger -> mute -> speakers.
	assert.ok(
		body.includes('_externalMerger.connect(_masterMuteGain)'),
		'if extroute bypasses the mute gain then ?muted=1 is silently ignored on the ' +
			'multichannel path and a routed deck plays out loud'
	);

	// Anything reaching the destination directly would route around the mute.
	assert.ok(
		!body.includes('_masterGain.connect(_ctx.destination)'),
		'if the master bus still reaches the destination directly then the mute is orphaned'
	);
	assert.ok(
		!body.includes('_externalMerger.connect(dest)'),
		'if the merger still reaches the destination directly then the mute is orphaned'
	);
});

test('graph build adopts the mute node rather than re-reading the URL', () => {
	const body = engineBlockAfter('function _ensureGraph(): AudioContext {');
	assert.ok(
		body.includes('attachMasterMuteNode(_masterMuteGain)'),
		'if the graph build does not attach the node then the standing mute is never applied'
	);
	assert.ok(
		!body.includes('startupMasterMuted()'),
		'if the graph build re-reads the URL then a runtime setMasterMuted made before ' +
			'the first AudioContext is silently clobbered back to the query-param value'
	);
});

test('teardown releases the mute node but keeps the mute engaged', () => {
	const body = engineBlockAfter('	async dispose(): Promise<void> {');
	assert.ok(
		body.includes('attachMasterMuteNode(null)'),
		'if dispose does not release the node then teardown leaks the old GainNode'
	);
	assert.ok(
		!body.includes('setMasterMuted(false)'),
		'if dispose unmutes then a route remount hands a headless agent its audio back'
	);
});
