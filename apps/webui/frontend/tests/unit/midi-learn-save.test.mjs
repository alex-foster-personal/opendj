// MIDI Learn save (#9781): recorded message bytes go through the same decoder
// the learn log uses, the armed learn binds the next control to the chosen
// command, and the binding is POSTed to a user map copy. No device, no browser:
// fetch is a stub and the bytes are recorded fixtures.
//
// Regression lines:
//   if a learned note is not POSTed to /maps/user-<port>/learned-binding then broken
//   if a built-in map is the save target instead of a user copy then broken
//   if a 409 conflict resends without confirmReplace saying yes then broken
//   if Escape while armed does not cancel learn without saving then broken

// requirement: CTRL-9781

import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let uiState;
let decode;
let learnSave;
let webmidi;
let calls;
let responses;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	uiState = await vite.ssrLoadModule('/src/lib/components/rb/midi/midi-ui-state.svelte.ts');
	decode = await vite.ssrLoadModule('/src/lib/rb/midi/decode.ts');
	learnSave = await vite.ssrLoadModule('/src/lib/rb/midi/learn-save.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
});

after(async () => {
	await vite?.close();
});

/** Stub fetch: record every call; POSTs answer from the `responses` queue, GETs list no maps. */
beforeEach(() => {
	calls = [];
	responses = [];
	globalThis.fetch = async (url, init = {}) => {
		calls.push({
			url: String(url),
			method: init.method ?? 'GET',
			body: init.body ? JSON.parse(init.body) : null
		});
		if ((init.method ?? 'GET') !== 'POST')
			return new Response(JSON.stringify({ maps: [] }), { status: 200 });
		const next = responses.shift() ?? { status: 200, body: {} };
		return new Response(JSON.stringify(next.body), { status: next.status });
	};
	uiState.cancelMidiLearn();
	webmidi._resetMidiForTests();
});

/** One learn-log row built from recorded wire bytes through the real decoder. */
function entryFromBytes([status, data1, data2], ts = 0) {
	return {
		ts,
		deviceId: 'port-1',
		deviceName: 'Test Pad 2',
		status,
		data1,
		data2,
		decoded: decode.decodeSource(status, data1),
		mapped: false,
		note: 'unmapped'
	};
}

/** Let the fire-and-forget save chain settle. */
async function settle() {
	for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
}

const posts = () => calls.filter((c) => c.method === 'POST');

test('learned note from recorded bytes is saved to a user map copy', async () => {
	await uiState.learnMidiCommand({ action: { type: 'deck_cue', deck: 1 } }, () => true);
	assert.equal(uiState.midiUi.learn.phase, 'armed');
	uiState.feedMidiLearnEntry(entryFromBytes([0x91, 0x3c, 0x7f]));
	await settle();
	assert.equal(uiState.midiUi.learn.phase, 'bound');
	assert.equal(posts().length, 1);
	const [post] = posts();
	assert.match(post.url, /\/api\/v1\/midi\/maps\/user-test-pad-2\/learned-binding$/);
	assert.deepEqual(post.body.binding.source, { ch: 2, kind: 'note', id: 0x3c });
	assert.deepEqual(post.body.binding.action, { type: 'deck_cue', deck: 1 });
	assert.equal(post.body.replace, false);
	assert.equal(post.body.template.nameMatch, '^Test Pad 2$');
});

test('built-in map is copied into the template, an installed map is targeted in place', () => {
	const builtin = {
		vendor: 'Acme',
		nameMatch: 'ACME-1',
		bindings: [
			{ source: { ch: 1, kind: 'note', id: 1 }, action: { type: 'deck_play_toggle', deck: 1 } }
		]
	};
	const copy = learnSave.learnSaveTarget('ACME-1 MIDI', builtin, null);
	assert.equal(copy.mapId, 'user-acme-1-midi');
	assert.equal(copy.template.nameMatch, 'ACME-1');
	assert.equal(copy.template.bindings.length, 1);
	assert.equal(copy.template.bindings[0].provenance.tier, 'hand');
	assert.equal(
		builtin.bindings[0].provenance,
		undefined,
		'the built-in map itself is never mutated'
	);
	assert.deepEqual(learnSave.learnSaveTarget('ACME-1 MIDI', builtin, 'my-acme'), {
		mapId: 'my-acme',
		template: null
	});
});

test('a 409 conflict asks to replace and only a yes resends with replace', async () => {
	const existing = {
		source: { ch: 1, kind: 'note', id: 60 },
		action: { type: 'deck_play_toggle', deck: 1 }
	};
	const target = { mapId: 'user-x', template: null };
	const binding = {
		source: { ch: 1, kind: 'note', id: 60 },
		action: { type: 'deck_cue', deck: 2 }
	};
	const asked = [];

	responses.push({ status: 409, body: { detail: { code: 'MIDI_LEARN_CONFLICT', existing } } });
	const no = await learnSave.saveLearnedBinding(target, binding, (e) => (asked.push(e), false));
	assert.equal(no.status, 'cancelled');
	assert.equal(posts().length, 1);

	responses.push({ status: 409, body: { detail: { code: 'MIDI_LEARN_CONFLICT', existing } } });
	responses.push({ status: 200, body: {} });
	const yes = await learnSave.saveLearnedBinding(target, binding, () => true);
	assert.equal(yes.status, 'saved');
	assert.equal(posts().length, 3);
	assert.equal(posts()[2].body.replace, true);
	assert.deepEqual(asked, [existing]);
});

test('Escape cancels an armed learn without saving', async () => {
	await uiState.learnMidiCommand({ action: { type: 'deck_cue', deck: 1 } }, () => true);
	assert.equal(uiState.handleMidiLearnKey('Escape'), true);
	assert.equal(uiState.midiUi.learn.phase, 'idle');
	uiState.feedMidiLearnEntry(entryFromBytes([0x90, 0x3c, 0x7f]));
	await settle();
	assert.equal(posts().length, 0);
	assert.equal(
		uiState.handleMidiLearnKey('Escape'),
		false,
		'Escape while idle is left to the drawer'
	);
});
