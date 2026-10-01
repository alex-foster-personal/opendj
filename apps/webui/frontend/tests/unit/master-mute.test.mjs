import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter, readFrontendSource as readSource } from './engine-source.mjs';
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
 * - if anything reaches the room delay without passing the mute gain then
 *   ?muted=1 plays out loud
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
// The mute is only a mute if every path to the destination runs through it.
// Since CUEOUT-14 the room delay line sits between the mute and the speakers,
// so the mute is the last GAIN rather than the last node; what still has to
// hold is that nothing reaches the destination around it. That is a property
// of how _ensureGraph strings the nodes together, and the engine never returns
// the wiring to a caller, so it is pinned as source text -- the same
// drift-guard shape control-explainer-phase-lock and load-memory-kpis use.
//-----------------------------------------------------------------------------

test('every path to the destination runs through the mute gain and the room delay', () => {
	const body = engineBlockAfter('function _ensureGraph(): AudioContext {');
	const topology = readSource('src/lib/rb/audio-output-topology.ts');

	// Internal path: master bus -> mute -> room delay -> speakers.
	assert.ok(
		topology.includes('masterMuteGain.connect(masterDelay)'),
		'if the mute gain does not feed the room delay then muting silences nothing'
	);
	assert.ok(
		topology.includes('masterDelay.connect(context.destination)'),
		'if the room delay does not feed _ctx.destination then the speakers are dead'
	);
	assert.ok(
		body.includes('wirePracticeBlendIntoMasterPath(_masterGain, _masterMuteGain, headphones)'),
		'if the master bus does not feed the mute gain then the mute is out of the chain'
	);
	assert.ok(
		body.includes('wireSplitCableIntoMasterPath(_masterGain, _masterMuteGain, headphones)'),
		'if the split-cable path does not feed the mute gain then split_cable is out of the chain'
	);

	// External-mixer path (?extroute=): merger -> mute -> room delay -> speakers.
	assert.ok(
		topology.includes('externalMerger.connect(masterMuteGain)'),
		'if extroute bypasses the mute gain then ?muted=1 is silently ignored on the ' +
			'multichannel path and a routed deck plays out loud'
	);
	assert.ok(
		topology.includes('masterDelay.connect(dest)'),
		'if the room delay does not feed the routed destination then extroute is dead'
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
	assert.ok(
		!topology.includes('masterMuteGain.connect(dest)') &&
			!topology.includes('masterMuteGain.connect(context.destination)'),
		'if the mute gain still reaches the destination directly then the room delay is ' +
			'bypassed and cue alignment is silently ignored on that path'
	);

	// Anything reaching the room delay directly would route around the mute.
	const masterDelayFeeders = [...topology.matchAll(/(\w+)\.connect\(masterDelay\)/g)].map(
		([, name]) => name
	);
	assert.ok(
		masterDelayFeeders.length > 0,
		'if nothing connects to the room delay then the mute-bypass guard is vacuous'
	);
	// master12-cue34 delays the master pair alone, so there the mute gain reaches
	// the room delay through a splitter and a two-channel merger. Each link of
	// that chain must be fed by the previous one and nothing else. The rendered
	// proof that a muted graph is silent on all four channels is the real-browser
	// spec tests/e2e/audio-output-topology.spec.ts.
	const feedersOf = (target) =>
		[...topology.matchAll(new RegExp(`(\\w+)\\.connect\\(${target}[,)]`, 'g'))].map(
			([, name]) => name
		);
	assert.deepEqual(
		[...new Set(masterDelayFeeders)].sort(),
		['masterMuteGain', 'roomMerger'],
		'if anything reaches the room delay without passing the mute gain then ?muted=1 plays out loud'
	);
	assert.deepEqual([...new Set(feedersOf('roomMerger'))], ['mutedSplitter']);
	assert.deepEqual([...new Set(feedersOf('mutedSplitter'))], ['masterMuteGain']);
	assert.deepEqual(
		[...new Set(feedersOf('outputMerger'))].sort(),
		['mutedSplitter', 'roomSplitter'],
		'if anything else feeds the four-channel output merger then it reaches the device unmuted'
	);
	assert.deepEqual([...new Set(feedersOf('roomSplitter'))], ['masterDelay']);
	assert.deepEqual(
		[...new Set(feedersOf('dest'))].sort(),
		['masterDelay', 'outputMerger'],
		'if anything else reaches the destination then it bypasses the mute'
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

//-----------------------------------------------------------------------------
// stored choice (UXR-01): a user's mute survives a reload
//-----------------------------------------------------------------------------

function fakeStorage(initial = {}) {
	const data = { ...initial };
	return {
		data,
		getItem: (key) => (key in data ? data[key] : null),
		setItem: (key, value) => {
			data[key] = String(value);
		},
		removeItem: (key) => {
			delete data[key];
		}
	};
}

async function freshModuleWith(search, storage) {
	globalThis.window = { location: { search }, localStorage: storage };
	return loadTypeScriptModule('src/lib/player/master-mute.svelte.ts');
}

test('a mute stored before a reload is still engaged after it, with no URL param', async () => {
	try {
		const fresh = await freshModuleWith('', fakeStorage({ [mute.MASTER_MUTE_STORAGE_KEY]: '1' }));
		assert.equal(fresh.isMasterMuted(), true, 'if a stored mute is dropped then a reload brings the audio back unasked');
	} finally {
		delete globalThis.window;
	}
});

test('muting stores exactly 1 and unmuting removes the key, so a reload after unmute is audible', async () => {
	const storage = fakeStorage();
	try {
		const fresh = await freshModuleWith('', storage);
		fresh.setMasterMuted(true);
		assert.deepEqual(storage.data, { [fresh.MASTER_MUTE_STORAGE_KEY]: '1' });
		fresh.setMasterMuted(false);
		assert.deepEqual(storage.data, {}, 'if unmute leaves a value behind then a later reload can come back muted');
		const reloaded = await freshModuleWith('', storage);
		assert.equal(reloaded.isMasterMuted(), false);
	} finally {
		delete globalThis.window;
	}
});

test('only a stored exact 1 mutes; any other stored value starts audible', () => {
	for (const raw of ['0', 'true', 'TRUE', '', ' 1', '1x', 'null']) {
		assert.equal(
			mute.readStoredMasterMuted(fakeStorage({ [mute.MASTER_MUTE_STORAGE_KEY]: raw })),
			false,
			`if a stored ${JSON.stringify(raw)} mutes then a corrupt value silences the headed client`
		);
	}
	assert.equal(mute.readStoredMasterMuted(fakeStorage()), false);
	assert.equal(mute.readStoredMasterMuted(null), false);
});

test('a stored unmute never overrides the ?muted=1 belt', async () => {
	try {
		const fresh = await freshModuleWith('?muted=1', fakeStorage());
		assert.equal(fresh.isMasterMuted(), true, 'if storage can veto ?muted=1 then headless agents play out loud');
	} finally {
		delete globalThis.window;
	}
});

test('storage that throws starts audible, keeps the in-memory mute, and says so', async () => {
	const boom = () => {
		throw new Error('SecurityError');
	};
	const warnings = [];
	const warn = console.warn;
	console.warn = (message) => warnings.push(message);
	try {
		const fresh = await freshModuleWith('', { getItem: boom, setItem: boom, removeItem: boom });
		assert.equal(fresh.isMasterMuted(), false);
		fresh.setMasterMuted(true);
		assert.equal(fresh.isMasterMuted(), true, 'if a storage failure blocks the mute then private mode cannot mute at all');
		assert.equal(warnings.length, 2, `expected a read and a write warning, got ${JSON.stringify(warnings)}`);
	} finally {
		console.warn = warn;
		delete globalThis.window;
	}
});
