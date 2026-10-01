// requirement STEM-48: the deck's stem row always names what the stems are
// doing, and a stem button is never silently inert.
//
// [if] stems are ready [then] no status label, tip says how to use the chips
// [if] the bundle is being fetched from the cloud [then] the label says
//   FETCHING STEMS with file progress and the tip carries the byte count
// [if] the decode is waiting on machine pressure [then] the label says so and
//   offers 'load_now'
// [if] the load failed [then] the label names the failure and offers 'retry'
// [if] the track has no stems anywhere [then] the tip says so in words
// [if] stems are switched off by a mode [then] the tip names the mode, and
//   does NOT claim the track has no stems
// [if] no track is loaded [then] the tip does not claim a track has no stems
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let view;
let graph;

before(async () => {
	view = await loadTypeScriptModule('src/lib/rb/stem-status.ts');
	graph = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

const ALIGNMENT = { sample_rate_hz: 44100, frame_count: 100, channel_count: 2, duration_ms: 2 };

test('ready stems show no status label and explain the chips', () => {
	const ready = graph.readyStemDeckState(
		{ source: 'demucs', model: 'htdemucs', layout: 'demucs4' },
		ALIGNMENT
	);
	const out = view.stemStatusView(ready);
	assert.equal(out.name, 'ready');
	assert.equal(out.label, '');
	assert.equal(out.action, null);
	assert.match(out.tip, /htdemucs/);
	assert.match(out.tip, /Shift\+click solo/);
});

test('a cloud fetch is named, with file progress in the label and bytes in the tip', () => {
	const fetching = graph.loadingStemDeckState({
		phase: 'fetching',
		progress: { files_total: 5, files_done: 2, bytes_done: 12_900_000 },
		reason: null
	});
	const out = view.stemStatusView(fetching);
	assert.equal(out.name, 'fetching');
	assert.equal(out.label, 'FETCHING STEMS 2/5');
	assert.equal(out.busy, true);
	assert.equal(out.action, null);
	assert.match(out.tip, /cloud/);
	assert.match(out.tip, /2 of 5 files/);
	assert.match(out.tip, /12\.3 MB/);
});

test('a fetch whose progress is not known yet is still named', () => {
	const out = view.stemStatusView(
		graph.loadingStemDeckState({ phase: 'fetching', progress: null, reason: null })
	);
	assert.equal(out.label, 'FETCHING STEMS');
	assert.match(out.tip, /cloud/);
});

test('every loading phase has its own name and a non-empty label', () => {
	const seen = new Set();
	for (const phase of ['probing', 'fetching', 'downloading', 'decoding', 'waiting', 'switching']) {
		const out = view.stemStatusView(
			graph.loadingStemDeckState({ phase, progress: null, reason: null })
		);
		assert.ok(out.label.length > 0, `${phase} renders no label: a silent inert row`);
		assert.ok(out.tip.length > 0);
		seen.add(out.label);
	}
	assert.equal(seen.size, 6, 'two phases share a label, so a reader cannot tell them apart');
});

test('a pressure wait says why and offers to load now', () => {
	const out = view.stemStatusView(
		graph.loadingStemDeckState({
			phase: 'waiting',
			progress: null,
			reason: 'this machine is under memory pressure while a deck is playing'
		})
	);
	assert.equal(out.name, 'waiting');
	assert.equal(out.action, 'load_now');
	assert.match(out.tip, /memory pressure/);
	assert.match(out.tip, /[Cc]lick/);
});

test('a failed load names the failure and offers a retry', () => {
	const failed = { ...graph.unavailableStemDeckState('hub answered HTTP 503'), status: 'error' };
	const out = view.stemStatusView(failed);
	assert.equal(out.name, 'error');
	assert.equal(out.action, 'retry');
	assert.match(out.label, /RETRY/);
	assert.match(out.tip, /hub answered HTTP 503/);
});

test('a track with no stems anywhere says so in words', () => {
	const out = view.stemStatusView(
		graph.unavailableStemDeckState("STEM_BUNDLE_NOT_FOUND: no stem bundle exists for 'x'")
	);
	assert.equal(out.name, 'none');
	assert.equal(out.action, null);
	assert.match(out.tip, /no stems/i);
	assert.match(out.tip, /this machine/);
	assert.match(out.tip, /cloud/);
});

test('stems switched off by a mode are not reported as a track without stems', () => {
	const out = view.stemStatusView(graph.unavailableStemDeckState('stems disabled: Trackify'));
	assert.equal(out.name, 'off');
	assert.match(out.tip, /Trackify/);
	assert.doesNotMatch(out.tip, /cloud/);
});

test('an empty deck does not claim a track has no stems', () => {
	const out = view.stemStatusView(graph.unavailableStemDeckState());
	assert.equal(out.name, 'empty');
	assert.doesNotMatch(out.tip, /cloud/);
});
