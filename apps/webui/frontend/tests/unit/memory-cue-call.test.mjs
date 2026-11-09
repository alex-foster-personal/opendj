// requirement: CUES-10013
// [if] memory_cue_call {deck, direction} is missing from the union or the IPC validator [then] broken
// [if] next does not pick the first memory cue strictly after the playhead, or prev the last strictly before [then] broken
// [if] none in that direction moves the deck instead of refusing with a reason [then] broken
// [if] the engine call does not set the cue point and seek exactly, without touching play state [then] broken
// [if] the deck CALL < / > buttons do not dispatch memory_cue_call [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const read = (rel) => readFileSync(`${SRC}/${rel}`, 'utf8');

let call;
let ipc;
let rust;

before(async () => {
	call = await loadTypeScriptModule('src/lib/rb/memory-cue-call.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	});
	rust = await loadTypeScriptModule('src/lib/audio-engine/rust-mode.svelte.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	});
});

const CUES = [
	{ kind: 'hot_cue', in_ms: 1000 },
	{ kind: 'memory', in_ms: 32000 },
	{ kind: 'memory', in_ms: 8000 },
	{ kind: 'loop', in_ms: 16000 },
	{ kind: 'memory', in_ms: 64000 }
];

describe('planMemoryCueCall', () => {
	test('next is the first memory cue strictly after the playhead, hot cues and loops ignored', () => {
		assert.deepEqual(call.planMemoryCueCall(CUES, 0, 'next'), { kind: 'call', cueMs: 8000 });
		assert.deepEqual(
			call.planMemoryCueCall(CUES, 8000, 'next'),
			{ kind: 'call', cueMs: 32000 },
			'strictly after'
		);
		assert.deepEqual(call.planMemoryCueCall(CUES, 10000, 'next'), { kind: 'call', cueMs: 32000 });
	});

	test('prev is the last memory cue strictly before the playhead', () => {
		assert.deepEqual(call.planMemoryCueCall(CUES, 70000, 'prev'), { kind: 'call', cueMs: 64000 });
		assert.deepEqual(
			call.planMemoryCueCall(CUES, 32000, 'prev'),
			{ kind: 'call', cueMs: 8000 },
			'strictly before'
		);
		assert.deepEqual(call.planMemoryCueCall(CUES, 32001, 'prev'), { kind: 'call', cueMs: 32000 });
	});

	test('none in that direction is a refusal with a reason, never a move', () => {
		assert.deepEqual(call.planMemoryCueCall(CUES, 64000, 'next'), {
			kind: 'refused',
			reason: 'no memory cue after the playhead'
		});
		assert.deepEqual(call.planMemoryCueCall(CUES, 8000, 'prev'), {
			kind: 'refused',
			reason: 'no memory cue before the playhead'
		});
		assert.deepEqual(call.planMemoryCueCall([{ kind: 'hot_cue', in_ms: 5 }], 0, 'next'), {
			kind: 'refused',
			reason: 'this track has no memory cues'
		});
		assert.throws(() => call.planMemoryCueCall(CUES, Number.NaN, 'next'), /positionMs/);
		assert.throws(() => call.planMemoryCueCall(CUES, 0, 'up'), /direction/);
	});
});

describe('memory_cue_call command', () => {
	test('the IPC validator takes {deck, direction} and refuses anything else by name', () => {
		assert.deepEqual(
			ipc.parsePerformanceCommandForTest({ type: 'memory_cue_call', deck: 2, direction: 'next' }),
			{
				type: 'memory_cue_call',
				deck: 2,
				direction: 'next'
			}
		);
		assert.deepEqual(
			ipc.parsePerformanceCommandForTest({ type: 'memory_cue_call', deck: 1, direction: 'prev' })
				.direction,
			'prev'
		);
		assert.throws(
			() =>
				ipc.parsePerformanceCommandForTest({ type: 'memory_cue_call', deck: 1, direction: 'back' }),
			/direction/
		);
		assert.throws(
			() => ipc.parsePerformanceCommandForTest({ type: 'memory_cue_call', deck: 1 }),
			/direction/
		);
		assert.throws(() =>
			ipc.parsePerformanceCommandForTest({
				type: 'memory_cue_call',
				deck: 1,
				direction: 'next',
				slot: 'A'
			})
		);
	});

	test('the engine sets the cue point, then seeks exactly, and never starts or stops the deck', () => {
		const body = engineBlockAfter(
			'async callMemoryCue(deck: DeckId, direction: MemoryCueCallDirection, pressT0Ms?: number): Promise<MemoryCueCallPlan> {'
		);
		assert.match(
			body,
			/planMemoryCueCall\(st\.anlz\?\.cues \?\? \[\], st\.position_ms, direction\)/
		);
		assert.match(body, /if \(plan\.kind === 'refused'\) return plan;/);
		const setCue = body.indexOf('st.cue_ms = plan.cueMs;');
		const seek = body.indexOf('await this.quantizedSeek(deck, plan.cueMs, true, pressT0Ms);');
		assert.ok(
			setCue >= 0 && seek > setCue,
			'cue point first, then an exact seek (quantizedSeek keeps play state)'
		);
		assert.doesNotMatch(body, /\.play\(|\.pause\(|playing\s*=/);
	});

	test('the dispatcher toasts a refusal instead of failing, and Rust engine mode refuses it by name', () => {
		const src = read('lib/rb/performance-ipc.svelte.ts');
		assert.match(
			src,
			/command\.type === 'memory_cue_call'\) \{\n\t\tconst plan = await engine\.callMemoryCue\(command\.deck, command\.direction, pressT0Ms\);\n[^\n]*\n\t\tif \(plan\.kind === 'refused'\) pushToast\(/
		);
		assert.ok(rust.WEB_AUDIO_ONLY.has('memory_cue_call'));
	});
});
