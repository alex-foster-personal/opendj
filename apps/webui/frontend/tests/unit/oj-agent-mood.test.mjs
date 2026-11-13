/**
 * OJ mood (issue #13937): the agent sidebar glass reacts to tool outcomes, through the pure
 * oj-agent-mood.ts module and an SSR render of OjAgentHeader.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mood;
let header;
let policy;
let ssr;

before(async () => {
	mood = await loadTypeScriptModule('src/lib/assistant/oj-agent-mood.ts');
	header = await loadTypeScriptModule('src/lib/components/assistant/oj-agent-header.ts');
	policy = await loadTypeScriptModule('src/lib/components/assistant/oj-motion-policy.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as OjAgentHeader } from '$lib/components/assistant/OjAgentHeader.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const NOW = 1_000_000;
const LIVE = {
	anyDeckPlaying: false,
	performanceVisible: false,
	reducedMotion: false,
	streaming: true
};
const READ_CALL = { id: 'c1', name: 'library_read', arguments: { path: '/api/v1/tracks' } };
const WRITE_CALL = { id: 'c2', name: 'library_write', arguments: { path: '/api/v1/playlists' } };
const ITEMS = [
	{ kind: 'user', text: 'make a playlist' },
	toolItem(READ_CALL, 'read'),
	toolItem(WRITE_CALL, 'confirm')
];
const ok = (call) => ({ type: 'tool_result', call_id: call.id, ok: true, result: {}, error: null });
const failed = (call, code) => ({
	type: 'tool_result',
	call_id: call.id,
	ok: false,
	result: null,
	error: { code, message: code }
});

function toolItem(call, gate) {
	return { kind: 'tool', call, gate, status: 'ok', reason: null, result: null, error: null };
}

function motionFor(input = LIVE) {
	return policy.ojMotionPolicy(input);
}

function beatFor(events, { motion = motionFor(), style = 'react' } = {}) {
	return mood.ojMoodBeat(events, { items: ITEMS, motion, style, now: NOW });
}

function baseLook(turn, motion = motionFor()) {
	return header.ojAgentLook(turn, motion, 'juice');
}

const IDLE_TURN = { items: ITEMS, busy: false, done: null };
const BUSY_TURN = { items: ITEMS, busy: true, done: null };
const LANDED_TURN = { items: ITEMS, busy: false, done: 'stop' };

function glassClasses(look) {
	const html = ssr.render(ssr.OjAgentHeader, { props: { look } }).body;
	return html.match(/<[^>]*class="(oj-glass[^"]*)"/)?.[1] ?? '';
}

test('an ok tool result smiles for one beat, then the glass is idle', () => {
	/** [if] a tool result event with ok arrives [then] the mood is smile for one beat, then idle, [else stop]. */
	const beat = beatFor([ok(READ_CALL)]);
	assert.deepEqual(beat, { glass: 'smile', burst: false, endsAt: NOW + mood.OJ_MOOD_BEAT_MS });
	const look = baseLook(IDLE_TURN);
	assert.equal(mood.ojMoodLook(look, beat, NOW, 'react').glass, 'smile');
	assert.equal(mood.ojMoodLook(look, beat, NOW + mood.OJ_MOOD_BEAT_MS - 1, 'react').glass, 'smile');
	assert.equal(mood.ojMoodLook(look, beat, NOW + mood.OJ_MOOD_BEAT_MS, 'react').glass, 'idle');
	assert.match(glassClasses(mood.ojMoodLook(look, beat, NOW, 'react')), /\bsmile\b/);
});

test('a declined or failed tool call, or a failed turn, sloshes', () => {
	/** [if] a tool call is declined or fails [then] the mood is slosh, [else stop]. */
	const events = [
		failed(WRITE_CALL, 'declined_by_user'),
		failed(READ_CALL, 'engine_error'),
		{ type: 'error', code: 'upstream_error', message: 'model down' }
	];
	for (const event of events) {
		const beat = beatFor([event]);
		assert.equal(beat?.glass, 'slosh', JSON.stringify(event));
		assert.equal(beat.burst, false);
		const look = mood.ojMoodLook(baseLook(BUSY_TURN), beat, NOW, 'react');
		assert.equal(look.glass, 'slosh');
		assert.match(glassClasses(look), /\bslosh\b/);
	}
});

test('a write that lands smiles with a bubble burst; reads and other events do not burst', () => {
	/** [if] a confirm-gated write lands ok [then] the glass smiles with bubbles for the beat, a read does not burst, [else stop]. */
	const burst = beatFor([ok(WRITE_CALL)]);
	assert.deepEqual(burst, { glass: 'smile', burst: true, endsAt: NOW + mood.OJ_MOOD_BEAT_MS });
	const look = mood.ojMoodLook(baseLook(IDLE_TURN), burst, NOW, 'react');
	assert.equal(look.glassBubbles, true);
	assert.equal(beatFor([ok(READ_CALL)]).burst, false);
	const quiet = [
		{ type: 'text', delta: 'ok' },
		{ type: 'tool_call', call: READ_CALL, gate: 'read' },
		{ type: 'done', reason: 'stop' }
	];
	assert.equal(beatFor(quiet), null);
	assert.equal(beatFor([ok(READ_CALL), failed(WRITE_CALL, 'engine_error')]).glass, 'slosh');
});

test('reduced motion or a playing deck on /performance never animates the mood', () => {
	/** [if] reduced motion is on or a deck plays on /performance [then] the mood never animates, [else stop]. */
	const stills = [
		{ ...LIVE, reducedMotion: true },
		{ ...LIVE, anyDeckPlaying: true, performanceVisible: true }
	];
	const liveBeat = beatFor([failed(READ_CALL, 'engine_error')]);
	for (const input of stills) {
		const motion = motionFor(input);
		assert.equal(beatFor([ok(WRITE_CALL)], { motion }), null, JSON.stringify(input));
		assert.equal(beatFor([failed(READ_CALL, 'engine_error')], { motion }), null);
		const look = baseLook(IDLE_TURN, motion);
		assert.equal(mood.ojMoodLook(look, liveBeat, NOW, 'react').glass, 'idle');
		assert.match(glassClasses(mood.ojMoodLook(look, liveBeat, NOW, 'react')), /\bstill\b/);
	}
});

test('the calm variant only ever produces think and idle', () => {
	/** [if] the variant is calm [then] only think and idle are ever produced, [else stop]. */
	const events = [
		ok(WRITE_CALL),
		failed(READ_CALL, 'declined_by_user'),
		{ type: 'error', code: 'x', message: 'x' }
	];
	for (const event of events) assert.equal(beatFor([event], { style: 'calm' }), null);
	const reactBeat = beatFor([ok(WRITE_CALL)]);
	const seen = new Set();
	for (const turn of [IDLE_TURN, BUSY_TURN, LANDED_TURN])
		for (const beat of [null, reactBeat])
			seen.add(mood.ojMoodLook(baseLook(turn), beat, NOW, 'calm').glass);
	assert.deepEqual([...seen].sort(), ['idle', 'think']);
});

test('the mood variant defaults to react, offers calm, and rejects unknown values', () => {
	/** [if] the mood variant is read [then] its id is new, its default is react with calm offered, and unknown throws, [else stop]. */
	assert.equal(mood.OJ_MOOD_VARIANT.id, 'variant_anim_oj_mood');
	assert.equal(mood.OJ_MOOD_VARIANT.defaultValue, 'react');
	assert.deepEqual(
		mood.OJ_MOOD_VARIANT.variants.map((v) => v.value),
		['react', 'calm']
	);
	assert.equal(mood.ojMoodStyle('calm'), 'calm');
	assert.throws(() => mood.ojMoodStyle('jumpy'), /react\|calm/);
	assert.ok(mood.OJ_MOOD_BEAT_MS >= 1_000, 'a beat timer stays at or above the 1 s OJ timer floor');
});
